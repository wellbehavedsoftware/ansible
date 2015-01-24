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

from ansible.playbook.attribute import Attribute, FieldAttribute
from ansible.playbook.base import Base
from ansible.playbook.conditional import Conditional
from ansible.playbook.helpers import load_list_of_tasks
from ansible.playbook.role import Role
from ansible.playbook.taggable import Taggable
from ansible.playbook.task_include import TaskInclude

class Block(Base, Conditional, Taggable):

    _block  = FieldAttribute(isa='list')
    _rescue = FieldAttribute(isa='list')
    _always = FieldAttribute(isa='list')

    # for future consideration? this would be functionally
    # similar to the 'else' clause for exceptions
    #_otherwise = FieldAttribute(isa='list')

    def __init__(self, parent_block=None, role=None, task_include=None, use_handlers=False):
        self._parent_block = parent_block
        self._role         = role
        self._task_include = task_include
        self._use_handlers = use_handlers

        super(Block, self).__init__()

    def get_vars(self):
        '''
        Blocks do not store variables directly, however they may be a member
        of a role or task include which does, so return those if present.
        '''

        all_vars = dict()

        if self._role:
            all_vars.update(self._role.get_vars())
        if self._task_include:
            all_vars.update(self._task_include.get_vars())

        return all_vars

    @staticmethod
    def load(data, parent_block=None, role=None, task_include=None, use_handlers=False, variable_manager=None, loader=None):
        b = Block(parent_block=parent_block, role=role, task_include=task_include, use_handlers=use_handlers)
        return b.load_data(data, variable_manager=variable_manager, loader=loader)

    def munge(self, ds):
        '''
        If a simple task is given, an implicit block for that single task
        is created, which goes in the main portion of the block
        '''
        is_block = False
        for attr in ('block', 'rescue', 'always'):
            if attr in ds:
                is_block = True
                break
        if not is_block:
            if isinstance(ds, list):
                return dict(block=ds)
            else:
                return dict(block=[ds])
        return ds

    def _load_block(self, attr, ds):
        return load_list_of_tasks(
            ds,
            block=self,
            role=self._role,
            task_include=self._task_include,
            variable_manager=self._variable_manager,
            loader=self._loader,
            use_handlers=self._use_handlers,
        )

    def _load_rescue(self, attr, ds):
        return load_list_of_tasks(
            ds,
            block=self,
            role=self._role,
            task_include=self._task_include,
            variable_manager=self._variable_manager,
            loader=self._loader,
            use_handlers=self._use_handlers,
        )

    def _load_always(self, attr, ds):
        return load_list_of_tasks(
            ds, 
            block=self, 
            role=self._role, 
            task_include=self._task_include,
            variable_manager=self._variable_manager, 
            loader=self._loader, 
            use_handlers=self._use_handlers,
        )

    # not currently used
    #def _load_otherwise(self, attr, ds):
    #    return load_list_of_tasks(
    #        ds, 
    #        block=self, 
    #        role=self._role, 
    #        task_include=self._task_include,
    #        variable_manager=self._variable_manager, 
    #        loader=self._loader, 
    #        use_handlers=self._use_handlers,
    #    )

    def compile(self):
        '''
        Returns the task list for this object
        '''

        task_list = []
        for task in self.block:
            # FIXME: evaulate task tags/conditionals here
            task_list.extend(task.compile())

        return task_list

    def copy(self):
        new_me = super(Block, self).copy()
        new_me._use_handlers = self._use_handlers

        new_me._parent_block = None
        if self._parent_block:
            new_me._parent_block = self._parent_block.copy()

        new_me._role = None
        if self._role:
            new_me._role = self._role

        new_me._task_include = None
        if self._task_include:
            new_me._task_include = self._task_include.copy()

        return new_me

    def serialize(self):
        '''
        Override of the default serialize method, since when we're serializing
        a task we don't want to include the attribute list of tasks.
        '''

        data = dict(when=self.when)

        if self._role is not None:
            data['role'] = self._role.serialize()
        if self._task_include is not None:
            data['task_include'] = self._task_include.serialize()

        return data

    def deserialize(self, data):
        '''
        Override of the default deserialize method, to match the above overridden
        serialize method
        '''

        from ansible.playbook.task_include import TaskInclude

        # unpack the when attribute, which is the only one we want
        self.when = data.get('when')

        # if there was a serialized role, unpack it too
        role_data = data.get('role')
        if role_data:
            r = Role()
            r.deserialize(role_data)
            self._role = r

        # if there was a serialized task include, unpack it too
        ti_data = data.get('task_include')
        if ti_data:
            ti = TaskInclude()
            ti.deserialize(ti_data)
            self._task_include = ti

    def evaluate_conditional(self, all_vars):
        if self._task_include is not None:
            if not self._task_include.evaluate_conditional(all_vars):
                return False
        if self._parent_block is not None:
            if not self._parent_block.evaluate_conditional(all_vars):
                return False
        elif self._role is not None:
            if not self._role.evaluate_conditional(all_vars):
                return False
        return super(Block, self).evaluate_conditional(all_vars)

    def evaluate_tags(self, only_tags, skip_tags, all_vars):
        result = False
        if self._parent_block is not None:
            result |= self._parent_block.evaluate_tags(only_tags=only_tags, skip_tags=skip_tags, all_vars=all_vars)
        elif self._role is not None:
            result |= self._role.evaluate_tags(only_tags=only_tags, skip_tags=skip_tags, all_vars=all_vars)
        return result | super(Block, self).evaluate_tags(only_tags=only_tags, skip_tags=skip_tags, all_vars=all_vars)

    def set_loader(self, loader):
        self._loader = loader
        if self._parent_block:
            self._parent_block.set_loader(loader)
        elif self._role:
            self._role.set_loader(loader)

        if self._task_include:
            self._task_include.set_loader(loader)

