# Copyright 2021 Huawei, Inc.
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

import collections
import sys

from neutron_lib.agent import l2_extension
from neutron_lib.callbacks import events as lib_events
from neutron_lib.callbacks import registry as lib_registry
from neutron_lib import context as lib_ctx
from oslo_log import log as logging

from neutron.api.rpc.callbacks.consumer import registry
from neutron.api.rpc.callbacks import events
from neutron.api.rpc.callbacks import resources
from neutron.api.rpc.handlers import resources_rpc
from neutron.plugins.ml2.drivers.openvswitch.agent.common import (
     constants as ovs_constants)

LOG = logging.getLogger(__name__)


class LocalIPAgentExtension(l2_extension.L2AgentExtension):
    SUPPORTED_RESOURCE_TYPES = [resources.LOCAL_IP_ASSOCIATION]

    def initialize(self, connection, driver_type):
        if driver_type != ovs_constants.EXTENSION_DRIVER_TYPE:
            LOG.error('Local IP extension is only supported for OVS, '
                      'currently uses %(driver_type)s',
                      {'driver_type': driver_type})
            sys.exit(1)

        self.resource_rpc = resources_rpc.ResourcesPullRpcApi()
        self._register_rpc_consumers(connection)

        self.local_ip_updates = {
            'added': collections.defaultdict(dict),
            'deleted': collections.defaultdict(dict)
        }

        self._pull_all_local_ip_associations()

    def _pull_all_local_ip_associations(self):
        context = lib_ctx.get_admin_context_without_session()

        assoc_list = self.resource_rpc.bulk_pull(
            context, resources.LOCAL_IP_ASSOCIATION)
        for assoc in assoc_list:
            port_id = assoc.fixed_port_id
            lip_id = assoc.local_ip_id
            self.local_ip_updates['added'][port_id][lip_id] = assoc
            # No need to notify "port updated" here as on restart agent
            # handles all ports anyway

    def consume_api(self, agent_api):
        """Allows an extension to gain access to resources internal to the
           neutron agent and otherwise unavailable to the extension.
        """
        self.agent_api = agent_api

    def _register_rpc_consumers(self, connection):
        """Allows an extension to receive notifications of updates made to
           items of interest.
        """
        endpoints = [resources_rpc.ResourcesPushRpcCallback()]
        for resource_type in self.SUPPORTED_RESOURCE_TYPES:
            # We assume that the neutron server always broadcasts the latest
            # version known to the agent
            registry.register(self._handle_notification, resource_type)
            topic = resources_rpc.resource_type_versioned_topic(resource_type)
            connection.create_consumer(topic, endpoints, fanout=True)

    def _handle_notification(self, context, resource_type,
                             local_ip_associations, event_type):
        if resource_type != resources.LOCAL_IP_ASSOCIATION:
            LOG.warning("Only Local IP Association notifications are "
                        "supported, got: %s", resource_type)
            return

        LOG.info("Local IP Association notification received: %s, %s",
                 local_ip_associations, event_type)
        for assoc in local_ip_associations:
            port_id = assoc.fixed_port_id
            lip_id = assoc.local_ip_id
            if event_type in [events.CREATED, events.UPDATED]:
                self.local_ip_updates['added'][port_id][lip_id] = assoc
            elif event_type == events.DELETED:
                self.local_ip_updates['deleted'][port_id][lip_id] = assoc
                self.local_ip_updates['added'][port_id].pop(lip_id, None)

            # Notify agent about port update to handle Local IP flows
            self._notify_port_updated(context, port_id)

    def _notify_port_updated(self, context, port_id):
        payload = lib_events.DBEventPayload(
            context, metadata={'changed_fields': {'local_ip'}},
            resource_id=port_id, states=(None,))
        lib_registry.publish(resources.PORT, lib_events.AFTER_UPDATE,
                             self, payload=payload)

    def handle_port(self, context, port):
        """Handle Local IP associations for a port.
        """
        port_id = port['port_id']
        local_ip_updates = self._pop_local_ip_updates_for_port(port_id)
        for assoc in local_ip_updates['added'].values():
            LOG.info("Local IP added for port %s: %s",
                     port_id, assoc.local_ip)
            # TBD
        for assoc in local_ip_updates['deleted'].values():
            LOG.info("Local IP deleted from port %s: %s",
                     port_id, assoc.local_ip)
            # TBD

    def _pop_local_ip_updates_for_port(self, port_id):
        return {
            'added': self.local_ip_updates['added'].pop(port_id, {}),
            'deleted': self.local_ip_updates['deleted'].pop(port_id, {})
        }

    def delete_port(self, context, port):
        self.local_ip_updates['added'].pop(port['port_id'], None)
        self.local_ip_updates['deleted'].pop(port['port_id'], None)
