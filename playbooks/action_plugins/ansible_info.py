from __future__ import annotations

import base64
import collections.abc
import json
import os
import pathlib
import sys

from ansible.module_utils.common.json import Direction, get_module_encoder
from ansible.plugins.action import ActionBase


class ActionModule(ActionBase):

    TRANSFERS_FILES = False
    _requires_connection = False

    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        del tmp

        extra_vars_paths = {}
        inventory_paths = {}

        is_inventory_file = False
        is_extra_var = False
        for a in sys.argv:
            if is_inventory_file:
                is_inventory_file = False
                inventory_paths[a] = pathlib.Path(a).read_text()

            elif is_extra_var:
                is_extra_var = False

                if a.startswith('@'):
                    e_path = a[1:]
                    extra_vars_paths[e_path] = pathlib.Path(e_path).read_text()

            elif a == '-e':
                is_extra_var = True

            elif a == '-i':
                is_inventory_file = True

        info = self.sanitise_dict({
            'argv': sys.argv,
            'become': self._connection.become.name if self._connection.become else None,
            'become_options': self._connection.become.get_options() if self._connection.become else None,
            'connection': self._connection.transport,
            'connection_options': self._connection.get_options(),
            'env': os.environ,
            'extra_var_contents': extra_vars_paths,
            'inventory_contents': inventory_paths,
            'task_args': self._task.args,
            'task_vars': task_vars,
        })

        json_encoder = get_module_encoder('legacy', Direction.CONTROLLER_TO_MODULE)
        result['b64'] = base64.b64encode(json.dumps(info, cls=json_encoder).encode()).decode()
        result['info'] = info

        return result

    def sanitise_dict(self, d: collections.abc.Mapping) -> dict:
        """
        Sanitise a dictionary by removing any sensitive values.
        """
        sanitised = {}
        for k, v in d.items():
            if isinstance(v, str):
                sanitised[k] = v
            elif isinstance(v, collections.abc.Mapping):
                sanitised[k] = self.sanitise_dict(v)
            elif isinstance(v, collections.abc.Iterable):
                sanitised[k] = [self.sanitise_dict(i) if isinstance(i, collections.abc.Mapping) else i for i in v]
            else:
                sanitised[k] = repr(v)

        return sanitised
