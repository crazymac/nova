# Copyright 2013 Rackspace Hosting
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from webob import exc

from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova.api.openstack import xmlutil
from nova import compute
from nova import exception

ALIAS = "os-instance-actions"
authorize_actions = extensions.extension_authorizer('compute',
                                                    'v3:' + ALIAS)
authorize_events = extensions.soft_extension_authorizer('compute',
                                                    'v3:' + ALIAS + ':events')

ACTION_KEYS = ['action', 'instance_uuid', 'request_id', 'user_id',
               'project_id', 'start_time', 'message']
EVENT_KEYS = ['event', 'start_time', 'finish_time', 'result', 'traceback']


def make_actions(elem):
    for key in ACTION_KEYS:
        elem.set(key)


def make_action(elem):
    for key in ACTION_KEYS:
        elem.set(key)
    event = xmlutil.TemplateElement('events', selector='events')
    for key in EVENT_KEYS:
        event.set(key)
    elem.append(event)


class InstanceActionsTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('instance_actions')
        elem = xmlutil.SubTemplateElement(root, 'instance_action',
                                          selector='instance_actions')
        make_actions(elem)
        return xmlutil.MasterTemplate(root, 1)


class InstanceActionTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('instance_action',
                                       selector='instance_action')
        make_action(root)
        return xmlutil.MasterTemplate(root, 1)


class InstanceActionsController(wsgi.Controller):

    def __init__(self):
        super(InstanceActionsController, self).__init__()
        self.compute_api = compute.API()
        self.action_api = compute.InstanceActionAPI()

    def _format_action(self, action_raw):
        action = {}
        for key in ACTION_KEYS:
            action[key] = action_raw.get(key)
        return action

    def _format_event(self, event_raw):
        event = {}
        for key in EVENT_KEYS:
            event[key] = event_raw.get(key)
        return event

    @wsgi.serializers(xml=InstanceActionsTemplate)
    def index(self, req, server_id):
        """Returns the list of actions recorded for a given instance."""
        context = req.environ["nova.context"]
        try:
            instance = self.compute_api.get(context, server_id)
        except exception.InstanceNotFound as err:
            raise exc.HTTPNotFound(explanation=err.format_message())
        authorize_actions(context, target=instance)
        actions_raw = self.action_api.actions_get(context, instance)
        actions = [self._format_action(action) for action in actions_raw]
        return {'instance_actions': actions}

    @wsgi.serializers(xml=InstanceActionTemplate)
    def show(self, req, server_id, id):
        """Return data about the given instance action."""
        context = req.environ['nova.context']
        try:
            instance = self.compute_api.get(context, server_id)
        except exception.InstanceNotFound as err:
            raise exc.HTTPNotFound(explanation=err.format_message())
        authorize_actions(context, target=instance)
        action = self.action_api.action_get_by_request_id(context, instance,
                                                          id)
        if action is None:
            msg = _("Action %s not found") % id
            raise exc.HTTPNotFound(msg)

        action_id = action['id']
        action = self._format_action(action)
        if authorize_events(context):
            events_raw = self.action_api.action_events_get(context, instance,
                                                           action_id)
            action['events'] = [self._format_event(evt) for evt in events_raw]
        return {'instance_action': action}


class InstanceActions(extensions.V3APIExtensionBase):
    """View a log of actions and events taken on an instance."""

    name = "InstanceActions"
    alias = ALIAS
    namespace = ("http://docs.openstack.org/compute/ext/"
                 "instance-actions/api/v3")
    version = 1

    def get_resources(self):
        ext = extensions.ResourceExtension('os-instance-actions',
                                           InstanceActionsController(),
                                           parent=dict(
                                               member_name='server',
                                               collection_name='servers'))
        return [ext]

    def get_controller_extensions(self):
        """It's an abstract function V3APIExtensionBase and the extension
        will not be loaded without it.
        """
        return []
