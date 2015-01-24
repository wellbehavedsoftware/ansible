# (c) 2012-2014, Michael DeHaan <michael.dehaan@gmail.com>
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

# Make coding more python3-ish
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from six import iteritems, string_types

import os

from hashlib import sha1
from types import NoneType

from ansible.errors import AnsibleError, AnsibleParserError
from ansible.parsing import DataLoader
from ansible.playbook.attribute import FieldAttribute
from ansible.playbook.base import Base
from ansible.playbook.conditional import Conditional
from ansible.playbook.helpers import load_list_of_blocks, compile_block_list
from ansible.playbook.role.include import RoleInclude
from ansible.playbook.role.metadata import RoleMetadata
from ansible.playbook.taggable import Taggable
from ansible.plugins import module_loader
from ansible.utils.vars import combine_vars


__all__ = ['Role', 'ROLE_CACHE']


# The role cache is used to prevent re-loading roles, which
# may already exist. Keys into this cache are the SHA1 hash
# of the role definition (for dictionary definitions, this
# will be based on the repr() of the dictionary object)
ROLE_CACHE = dict()


class Role(Base, Conditional, Taggable):

    def __init__(self):
        self._role_name        = None
        self._role_path        = None
        self._role_params      = dict()
        self._loader           = None

        self._metadata         = None
        self._parents          = []
        self._dependencies     = []
        self._task_blocks      = []
        self._handler_blocks   = []
        self._default_vars     = dict()
        self._role_vars        = dict()
        self._had_task_run     = False
        self._completed        = False

        super(Role, self).__init__()

    def __repr__(self):
        return self.get_name()

    def get_name(self):
        return self._role_name

    @staticmethod
    def load(role_include, parent_role=None):
        # FIXME: add back in the role caching support
        try:
            # The ROLE_CACHE is a dictionary of role names, with each entry
            # containing another dictionary corresponding to a set of parameters
            # specified for a role as the key and the Role() object itself.
            # We use frozenset to make the dictionary hashable.

            hashed_params = frozenset(role_include.get_role_params().iteritems())
            if role_include.role in ROLE_CACHE:
                for (entry, role_obj) in ROLE_CACHE[role_include.role].iteritems():
                    if hashed_params == entry:
                        if parent_role:
                            role_obj.add_parent(parent_role)
                        return role_obj

            r = Role()
            r._load_role_data(role_include, parent_role=parent_role)

            if role_include.role not in ROLE_CACHE:
                ROLE_CACHE[role_include.role] = dict()

            ROLE_CACHE[role_include.role][hashed_params] = r
            return r

        except RuntimeError:
            # FIXME: needs a better way to access the ds in the role include
            raise AnsibleError("A recursion loop was detected with the roles specified. Make sure child roles do not have dependencies on parent roles", obj=role_include._ds)

    def _load_role_data(self, role_include, parent_role=None):
        self._role_name        = role_include.role
        self._role_path        = role_include.get_role_path()
        self._role_params      = role_include.get_role_params()
        self._variable_manager = role_include.get_variable_manager()
        self._loader           = role_include.get_loader()

        if parent_role:
            self.add_parent(parent_role)

        current_when = getattr(self, 'when')[:]
        current_when.extend(role_include.when)
        setattr(self, 'when', current_when)
        
        current_tags = getattr(self, 'tags')[:]
        current_tags.extend(role_include.tags)
        setattr(self, 'tags', current_tags)

        # save the current base directory for the loader and set it to the current role path
        #cur_basedir = self._loader.get_basedir()
        #self._loader.set_basedir(self._role_path)

        # load the role's files, if they exist
        library = os.path.join(self._role_path, 'library')
        if os.path.isdir(library):
            module_loader.add_directory(library)

        metadata = self._load_role_yaml('meta')
        if metadata:
            self._metadata = RoleMetadata.load(metadata, owner=self, loader=self._loader)
            self._dependencies = self._load_dependencies()

        task_data = self._load_role_yaml('tasks')
        if task_data:
            self._task_blocks = load_list_of_blocks(task_data, role=self, loader=self._loader)

        handler_data = self._load_role_yaml('handlers')
        if handler_data:
            self._handler_blocks = load_list_of_blocks(handler_data, role=self, loader=self._loader)

        # vars and default vars are regular dictionaries
        self._role_vars  = self._load_role_yaml('vars')
        if not isinstance(self._role_vars, (dict, NoneType)):
            raise AnsibleParserError("The vars/main.yml file for role '%s' must contain a dictionary of variables" % self._role_name, obj=ds)
        elif self._role_vars is None:
            self._role_vars = dict()

        self._default_vars = self._load_role_yaml('defaults')
        if not isinstance(self._default_vars, (dict, NoneType)):
            raise AnsibleParserError("The default/main.yml file for role '%s' must contain a dictionary of variables" % self._role_name, obj=ds)
        elif self._default_vars is None:
            self._default_vars = dict()

        # and finally restore the previous base directory
        #self._loader.set_basedir(cur_basedir)

    def _load_role_yaml(self, subdir):
        file_path = os.path.join(self._role_path, subdir)
        if self._loader.path_exists(file_path) and self._loader.is_directory(file_path):
            main_file = self._resolve_main(file_path)
            if self._loader.path_exists(main_file):
                return self._loader.load_from_file(main_file)
        return None

    def _resolve_main(self, basepath):
        ''' flexibly handle variations in main filenames '''
        possible_mains = (
            os.path.join(basepath, 'main.yml'),
            os.path.join(basepath, 'main.yaml'),
            os.path.join(basepath, 'main.json'),
            os.path.join(basepath, 'main'),
        )

        if sum([self._loader.is_file(x) for x in possible_mains]) > 1:
            raise AnsibleError("found multiple main files at %s, only one allowed" % (basepath))
        else:
            for m in possible_mains:
                if self._loader.is_file(m):
                    return m # exactly one main file
            return possible_mains[0] # zero mains (we still need to return something)

    def _load_dependencies(self):
        '''
        Recursively loads role dependencies from the metadata list of
        dependencies, if it exists
        '''

        deps = []
        if self._metadata:
            for role_include in self._metadata.dependencies:
                r = Role.load(role_include, parent_role=self)
                deps.append(r)

        return deps

    #------------------------------------------------------------------------------
    # other functions

    def add_parent(self, parent_role):
        ''' adds a role to the list of this roles parents '''
        assert isinstance(parent_role, Role)

        if parent_role not in self._parents:
            self._parents.append(parent_role)

    def get_parents(self):
        return self._parents

    def get_default_vars(self):
        # FIXME: get these from dependent roles too
        default_vars = dict()
        for dep in self.get_all_dependencies():
            default_vars = combine_vars(default_vars, dep.get_default_vars())
        default_vars = combine_vars(default_vars, self._default_vars)
        return default_vars

    def get_inherited_vars(self):
        inherited_vars = dict()
        for parent in self._parents:
            inherited_vars = combine_vars(inherited_vars, parent.get_inherited_vars())
            inherited_vars = combine_vars(inherited_vars, parent._role_vars)
            inherited_vars = combine_vars(inherited_vars, parent._role_params)
        return inherited_vars

    def get_vars(self):
        all_vars = self.get_inherited_vars()

        for dep in self.get_all_dependencies():
            all_vars = combine_vars(all_vars, dep.get_vars())

        all_vars = combine_vars(all_vars, self._role_vars)
        all_vars = combine_vars(all_vars, self._role_params)

        return all_vars

    def get_direct_dependencies(self):
        return self._dependencies[:]

    def get_all_dependencies(self):
        '''
        Returns a list of all deps, built recursively from all child dependencies,
        in the proper order in which they should be executed or evaluated.
        '''

        child_deps  = []

        for dep in self.get_direct_dependencies():
            for child_dep in dep.get_all_dependencies():
                child_deps.append(child_dep)
            child_deps.append(dep)

        return child_deps

    def get_task_blocks(self):
        return self._task_blocks[:]

    def get_handler_blocks(self):
        return self._handler_blocks[:]

    def has_run(self):
        '''
        Returns true if this role has been iterated over completely and
        at least one task was run
        '''

        return self._had_task_run and self._completed

    def compile(self, dep_chain=[]):
        '''
        Returns the task list for this role, which is created by first
        recursively compiling the tasks for all direct dependencies, and
        then adding on the tasks for this role.

        The role compile() also remembers and saves the dependency chain
        with each task, so tasks know by which route they were found, and
        can correctly take their parent's tags/conditionals into account.
        '''

        task_list = []

        # update the dependency chain here
        new_dep_chain = dep_chain + [self]

        deps = self.get_direct_dependencies()
        for dep in deps:
            dep_tasks = dep.compile(dep_chain=new_dep_chain)
            for dep_task in dep_tasks:
                # since we're modifying the task, and need it to be unique,
                # we make a copy of it here and assign the dependency chain
                # to the copy, then append the copy to the task list.
                new_dep_task = dep_task.copy()
                new_dep_task._dep_chain = new_dep_chain
                task_list.append(new_dep_task)

        task_list.extend(compile_block_list(self._task_blocks))

        return task_list

    def serialize(self, include_deps=True):
        res = super(Role, self).serialize()

        res['_role_name']    = self._role_name
        res['_role_path']    = self._role_path
        res['_role_vars']    = self._role_vars
        res['_role_params']  = self._role_params
        res['_default_vars'] = self._default_vars
        res['_had_task_run'] = self._had_task_run
        res['_completed']    = self._completed

        if self._metadata:
            res['_metadata'] = self._metadata.serialize()

        if include_deps:
            deps = []
            for role in self.get_direct_dependencies():
                deps.append(role.serialize())
            res['_dependencies'] = deps

        parents = []
        for parent in self._parents:
            parents.append(parent.serialize(include_deps=False))
        res['_parents'] = parents

        return res

    def deserialize(self, data, include_deps=True):
        self._role_name    = data.get('_role_name', '')
        self._role_path    = data.get('_role_path', '')
	self._role_vars    = data.get('_role_vars', dict())
        self._role_params  = data.get('_role_params', dict())
        self._default_vars = data.get('_default_vars', dict())
        self._had_task_run = data.get('_had_task_run', False)
        self._completed    = data.get('_completed', False)

        if include_deps:
            deps = []
            for dep in data.get('_dependencies', []):
                r = Role()
                r.deserialize(dep)
                deps.append(r)
            setattr(self, '_dependencies', deps)

        parent_data = data.get('_parents', [])
        parents = []
        for parent in parent_data:
            r = Role()
            r.deserialize(parent, include_deps=False)
            parents.append(r)
        setattr(self, '_parents', parents)

        metadata_data = data.get('_metadata')
        if metadata_data:
            m = RoleMetadata()
            m.deserialize(metadata_data)
            self._metadata = m

        super(Role, self).deserialize(data)

    def set_loader(self, loader):
        self._loader = loader
        for parent in self._parents:
            parent.set_loader(loader)
        for dep in self.get_direct_dependencies():
            dep.set_loader(loader)

