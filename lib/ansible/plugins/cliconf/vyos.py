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
import json

from itertools import chain

from ansible.errors import AnsibleConnectionFailure
from ansible.module_utils._text import to_text
from ansible.module_utils.network.common.config import NetworkConfig, dumps
from ansible.module_utils.network.common.utils import to_list
from ansible.plugins.cliconf import CliconfBase


class Cliconf(CliconfBase):

    def get_device_info(self):
        device_info = {}

        device_info['network_os'] = 'vyos'
        reply = self.get('show version')
        data = to_text(reply, errors='surrogate_or_strict').strip()

        match = re.search(r'Version:\s*(\S+)', data)
        if match:
            device_info['network_os_version'] = match.group(1)

        match = re.search(r'HW model:\s*(\S+)', data)
        if match:
            device_info['network_os_model'] = match.group(1)

        reply = self.get('show host name')
        device_info['network_os_hostname'] = to_text(reply, errors='surrogate_or_strict').strip()

        return device_info

    def get_config(self, filter=None, format='set'):
        if format == 'text':
            out = self.send_command('show configuration')
        else:
            out = self.send_command('show configuration commands')
        return out

    def edit_config(self, candidate=None, commit=True, replace=False, comment=None):
        resp = {}
        if not candidate:
            raise ValueError('must provide a candidate config to load')

        if commit not in (True, False):
            raise ValueError("'commit' must be a bool, got %s" % commit)

        if replace not in (True, False):
            raise ValueError("'replace' must be a bool, got %s" % replace)

        operations = self.get_device_operations()
        if replace and not operations['supports_replace']:
            raise ValueError("configuration replace is not supported on vyos")

        results = []

        for cmd in chain(['configure'], to_list(candidate)):
            if not isinstance(cmd, collections.Mapping):
                cmd = {'command': cmd}

            results.append(self.send_command(**cmd))

        out = self.get('compare')
        out = to_text(out, errors='surrogate_or_strict')
        diff_config = out if not out.startswith('No changes') else None

        if diff_config:
            if commit:
                try:
                    self.commit(comment)
                except AnsibleConnectionFailure as e:
                    msg = 'commit failed: %s' % e.message
                    self.discard_changes()
                    raise AnsibleConnectionFailure(msg)
                else:
                    self.get('exit')
            else:
                self.discard_changes()
        else:
            self.get('exit')

        resp['diff'] = diff_config
        resp['response'] = results[1:-1]
        return json.dumps(resp)

    def get(self, command=None, prompt=None, answer=None, sendonly=False):
        if not command:
            raise ValueError('must provide value of command to execute')
        return self.send_command(command, prompt=prompt, answer=answer, sendonly=sendonly)

    def commit(self, comment=None):
        if comment:
            command = 'commit comment "{0}"'.format(comment)
        else:
            command = 'commit'
        self.send_command(command)

    def discard_changes(self):
        self.send_command('exit discard')

    def get_diff(self, candidate=None, running=None, match='line', diff_ignore_lines=None, path=None, replace=None):
        diff = {}
        device_operations = self.get_device_operations()
        option_values = self.get_option_values()

        if candidate is None and not device_operations['supports_onbox_diff']:
            raise ValueError("candidate configuration is required to generate diff")

        if match not in option_values['diff_match']:
            raise ValueError("'match' value %s in invalid, valid values are %s" % (match, option_values['diff_match']))

        if replace:
            raise ValueError("'replace' in diff is not supported on vyos")

        if diff_ignore_lines:
            raise ValueError("'diff_ignore_lines' in diff is not supported on vyos")

        if path:
            raise ValueError("'path' in diff is not supported on vyos")

        set_format = candidate.startswith('set') or candidate.startswith('delete')
        candidate_obj = NetworkConfig(indent=4, contents=candidate)
        if not set_format:
            config = [c.line for c in candidate_obj.items]
            commands = list()
            # this filters out less specific lines
            for item in config:
                for index, entry in enumerate(commands):
                    if item.startswith(entry):
                        del commands[index]
                        break
                commands.append(item)

            candidate_commands = ['set %s' % cmd.replace(' {', '') for cmd in commands]

        else:
            candidate_commands = str(candidate).strip().split('\n')

        if match == 'none':
            diff['config_diff'] = list(candidate_commands)
            return json.dumps(diff)

        running_commands = [str(c).replace("'", '') for c in running.splitlines()]

        updates = list()
        visited = set()

        for line in candidate_commands:
            item = str(line).replace("'", '')

            if not item.startswith('set') and not item.startswith('delete'):
                raise ValueError('line must start with either `set` or `delete`')

            elif item.startswith('set') and item not in running_commands:
                updates.append(line)

            elif item.startswith('delete'):
                if not running_commands:
                    updates.append(line)
                else:
                    item = re.sub(r'delete', 'set', item)
                    for entry in running_commands:
                        if entry.startswith(item) and line not in visited:
                            updates.append(line)
                            visited.add(line)

        diff['config_diff'] = list(updates)
        return json.dumps(diff)

    def get_device_operations(self):
        return {
            'supports_diff_replace': False,
            'supports_commit': True,
            'supports_rollback': True,
            'supports_defaults': False,
            'supports_onbox_diff': False,
            'supports_commit_comment': True,
            'supports_multiline_delimiter': False,
            'support_diff_match': True,
            'support_diff_ignore_lines': False,
            'supports_generate_diff': True,
            'supports_replace': False
        }

    def get_option_values(self):
        return {
            'format': ['set', 'text'],
            'diff_match': ['line', 'none'],
            'diff_replace': [],
        }

    def get_capabilities(self):
        result = {}
        result['rpc'] = self.get_base_rpc() + ['commit', 'discard_changes', 'get_diff']
        result['network_api'] = 'cliconf'
        result['device_info'] = self.get_device_info()
        result['device_operations'] = self.get_device_operations()
        result.update(self.get_option_values())
        return json.dumps(result)
