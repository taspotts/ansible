#
# (c) 2017 Red Hat Inc.
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.
#
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import collections
import re
import time
import json

from itertools import chain

from ansible.module_utils._text import to_text
from ansible.module_utils.six import iteritems
from ansible.module_utils.network.common.config import NetworkConfig, dumps
from ansible.module_utils.network.common.utils import to_list
from ansible.plugins.cliconf import CliconfBase, enable_mode


class Cliconf(CliconfBase):

    @enable_mode
    def get_config(self, source='running', filter=None, format='text'):
        if source not in ('running', 'startup'):
            return self.invalid_params("fetching configuration from %s is not supported" % source)

        if not filter:
            filter = []
        if source == 'running':
            cmd = 'show running-config '
        else:
            cmd = 'show startup-config '

        cmd += ' '.join(to_list(filter))
        cmd = cmd.strip()

        return self.send_command(cmd)

    def get_diff(self, candidate=None, running=None, match='line', diff_ignore_lines=None, path=None, replace='line'):
        """
        Generate diff between candidate and running configuration. If the
        remote host supports onbox diff capabilities ie. supports_onbox_diff in that case
        candidate and running configurations are not required to be passed as argument.
        In case if onbox diff capability is not supported candidate argument is mandatory
        and running argument is optional.
        :param candidate: The configuration which is expected to be present on remote host.
        :param running: The base configuration which is used to generate diff.
        :param match: Instructs how to match the candidate configuration with current device configuration
                      Valid values are 'line', 'strict', 'exact', 'none'.
                      'line' - commands are matched line by line
                      'strict' - command lines are matched with respect to position
                      'exact' - command lines must be an equal match
                      'none' - will not compare the candidate configuration with the running configuration
        :param diff_ignore_lines: Use this argument to specify one or more lines that should be
                                  ignored during the diff.  This is used for lines in the configuration
                                  that are automatically updated by the system.  This argument takes
                                  a list of regular expressions or exact line matches.
        :param path: The ordered set of parents that uniquely identify the section or hierarchy
                     the commands should be checked against.  If the parents argument
                     is omitted, the commands are checked against the set of top
                    level or global commands.
        :param replace: Instructs on the way to perform the configuration on the device.
                        If the replace argument is set to I(line) then the modified lines are
                        pushed to the device in configuration mode.  If the replace argument is
                        set to I(block) then the entire command block is pushed to the device in
                        configuration mode if any line is not correct.
        :return: Configuration diff in  json format.
               {
                   'config_diff': '',
                   'banner_diff': ''
               }

        """
        diff = {}
        device_operations = self.get_device_operations()
        option_values = self.get_option_values()

        if candidate is None and not device_operations['supports_onbox_diff']:
            raise ValueError("candidate configuration is required to generate diff")

        if match not in option_values['diff_match']:
            raise ValueError("'match' value %s in invalid, valid values are %s" % (match, option_values['diff_match']))

        if replace not in option_values['diff_replace']:
            raise ValueError("'replace' value %s in invalid, valid values are %s" % (replace, option_values['diff_replace']))

        # prepare candidate configuration
        candidate_obj = NetworkConfig(indent=1)
        want_src, want_banners = self._extract_banners(candidate)
        candidate_obj.load(want_src)

        if running and match != 'none':
            # running configuration
            have_src, have_banners = self._extract_banners(running)
            running_obj = NetworkConfig(indent=1, contents=have_src, ignore_lines=diff_ignore_lines)
            configdiffobjs = candidate_obj.difference(running_obj, path=path, match=match, replace=replace)

        else:
            configdiffobjs = candidate_obj.items
            have_banners = {}

        configdiff = dumps(configdiffobjs, 'commands') if configdiffobjs else ''
        diff['config_diff'] = configdiff if configdiffobjs else {}

        banners = self._diff_banners(want_banners, have_banners)

        diff['banner_diff'] = banners if banners else {}
        return json.dumps(diff)

    @enable_mode
    def edit_config(self, candidate=None, commit=True, replace=False, comment=None):
        resp = {}
        if not candidate:
            raise ValueError("must provide a candidate config to load")

        if commit not in (True, False):
            raise ValueError("'commit' must be a bool, got %s" % commit)

        if replace not in (True, False):
            raise ValueError("'replace' must be a bool, got %s" % replace)

        operations = self.get_device_operations()
        if replace and not operations['supports_replace']:
            raise ValueError("configuration replace is not supported on ios")

        results = []
        if commit:
            for line in chain(['configure terminal'], to_list(candidate)):
                if not isinstance(line, collections.Mapping):
                    line = {'command': line}

                cmd = line['command']
                if cmd != 'end' and cmd[0] != '!':
                    results.append(self.send_command(**line))

            results.append(self.send_command('end'))

        resp['response'] = results[1:-1]
        return json.dumps(resp)

    def get(self, command=None, prompt=None, answer=None, sendonly=False):
        if not command:
            raise ValueError('must provide value of command to execute')
        return self.send_command(command=command, prompt=prompt, answer=answer, sendonly=sendonly)

    def get_device_info(self):
        device_info = {}

        device_info['network_os'] = 'ios'
        reply = self.get(command='show version')
        data = to_text(reply, errors='surrogate_or_strict').strip()

        match = re.search(r'Version (\S+)', data)
        if match:
            device_info['network_os_version'] = match.group(1).strip(',')

        match = re.search(r'^Cisco (.+) \(revision', data, re.M)
        if match:
            device_info['network_os_model'] = match.group(1)

        match = re.search(r'^(.+) uptime', data, re.M)
        if match:
            device_info['network_os_hostname'] = match.group(1)

        return device_info

    def get_device_operations(self):
        return {
            'supports_diff_replace': True,
            'supports_commit': False,
            'supports_rollback': False,
            'supports_defaults': True,
            'supports_onbox_diff': False,
            'supports_commit_comment': False,
            'supports_multiline_delimiter': False,
            'support_diff_match': True,
            'support_diff_ignore_lines': True,
            'supports_generate_diff': True,
            'supports_replace': False
        }

    def get_option_values(self):
        return {
            'format': ['text'],
            'diff_match': ['line', 'strict', 'exact', 'none'],
            'diff_replace': ['line', 'block']
        }

    def get_capabilities(self):
        result = dict()
        result['rpc'] = self.get_base_rpc() + ['edit_banner', 'get_diff']
        result['network_api'] = 'cliconf'
        result['device_info'] = self.get_device_info()
        result['device_operations'] = self.get_device_operations()
        result.update(self.get_option_values())
        return json.dumps(result)

    def edit_banner(self, candidate=None, multiline_delimiter="@", commit=True, diff=False):
        """
        Edit banner on remote device
        :param banners: Banners to be loaded in json format
        :param multiline_delimiter: Line delimiter for banner
        :param commit: Boolean value that indicates if the device candidate
               configuration should be  pushed in the running configuration or discarded.
        :param diff: Boolean flag to indicate if configuration that is applied on remote host should
                     generated and returned in response or not
        :return: Returns response of executing the configuration command received
             from remote host
        """
        banners_obj = json.loads(candidate)
        results = []
        if commit:
            for key, value in iteritems(banners_obj):
                key += ' %s' % multiline_delimiter
                for cmd in ['config terminal', key, value, multiline_delimiter, 'end']:
                    obj = {'command': cmd, 'sendonly': True}
                    results.append(self.send_command(**obj))

                time.sleep(0.1)
                results.append(self.send_command('\n'))

        diff_banner = None
        if diff:
            diff_banner = candidate

        return diff_banner, results[1:-1]

    def _extract_banners(self, config):
        banners = {}
        banner_cmds = re.findall(r'^banner (\w+)', config, re.M)
        for cmd in banner_cmds:
            regex = r'banner %s \^C(.+?)(?=\^C)' % cmd
            match = re.search(regex, config, re.S)
            if match:
                key = 'banner %s' % cmd
                banners[key] = match.group(1).strip()

        for cmd in banner_cmds:
            regex = r'banner %s \^C(.+?)(?=\^C)' % cmd
            match = re.search(regex, config, re.S)
            if match:
                config = config.replace(str(match.group(1)), '')

        config = re.sub(r'banner \w+ \^C\^C', '!! banner removed', config)
        return config, banners

    def _diff_banners(self, want, have):
        candidate = {}
        for key, value in iteritems(want):
            if value != have.get(key):
                candidate[key] = value
        return candidate
