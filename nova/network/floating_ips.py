# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2011 X.commerce, a business unit of eBay Inc.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
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

from oslo.config import cfg

from nova import context
from nova.db import base
from nova import exception
from nova.network import rpcapi as network_rpcapi
from nova.openstack.common import excutils
from nova.openstack.common import importutils
from nova.openstack.common import log as logging
from nova.openstack.common.notifier import api as notifier
from nova.openstack.common import processutils
from nova.openstack.common.rpc import common as rpc_common
from nova.openstack.common import uuidutils
from nova import quota
from nova import servicegroup
from nova import utils

LOG = logging.getLogger(__name__)

QUOTAS = quota.QUOTAS

floating_opts = [
    cfg.StrOpt('default_floating_pool',
               default='nova',
               help='Default pool for floating ips'),
    cfg.BoolOpt('auto_assign_floating_ip',
                default=False,
                help='Autoassigning floating ip to VM'),
    cfg.StrOpt('floating_ip_dns_manager',
               default='nova.network.noop_dns_driver.NoopDNSDriver',
               help='full class name for the DNS Manager for floating IPs'),
    cfg.StrOpt('instance_dns_manager',
               default='nova.network.noop_dns_driver.NoopDNSDriver',
               help='full class name for the DNS Manager for instance IPs'),
    cfg.StrOpt('instance_dns_domain',
               default='',
               help='full class name for the DNS Zone for instance IPs'),
]

CONF = cfg.CONF
CONF.register_opts(floating_opts)
CONF.import_opt('public_interface', 'nova.network.linux_net')
CONF.import_opt('network_topic', 'nova.network.rpcapi')


class FloatingIP(object):
    """Mixin class for adding floating IP functionality to a manager."""

    servicegroup_api = None

    def init_host_floating_ips(self):
        """Configures floating ips owned by host."""

        admin_context = context.get_admin_context()
        try:
            floating_ips = self.db.floating_ip_get_all_by_host(admin_context,
                                                               self.host)
        except exception.NotFound:
            return

        for floating_ip in floating_ips:
            fixed_ip_id = floating_ip.get('fixed_ip_id')
            if fixed_ip_id:
                try:
                    fixed_ip = self.db.fixed_ip_get(admin_context,
                                                    fixed_ip_id,
                                                    get_network=True)
                except exception.FixedIpNotFound:
                    msg = _('Fixed ip %(fixed_ip_id)s not found') % locals()
                    LOG.debug(msg)
                    continue
                interface = CONF.public_interface or floating_ip['interface']
                try:
                    self.l3driver.add_floating_ip(floating_ip['address'],
                                                  fixed_ip['address'],
                                                  interface,
                                                  fixed_ip['network'])
                except processutils.ProcessExecutionError:
                    LOG.debug(_('Interface %(interface)s not found'), locals())
                    raise exception.NoFloatingIpInterface(interface=interface)

    def allocate_for_instance(self, context, **kwargs):
        """Handles allocating the floating IP resources for an instance.

        calls super class allocate_for_instance() as well

        rpc.called by network_api
        """
        instance_uuid = kwargs.get('instance_id')
        if not uuidutils.is_uuid_like(instance_uuid):
            instance_uuid = kwargs.get('instance_uuid')
        project_id = kwargs.get('project_id')
        requested_networks = kwargs.get('requested_networks')
        # call the next inherited class's allocate_for_instance()
        # which is currently the NetworkManager version
        # do this first so fixed ip is already allocated
        nw_info = super(FloatingIP, self).allocate_for_instance(context,
                                                                **kwargs)
        if CONF.auto_assign_floating_ip:
            # allocate a floating ip
            floating_address = self.allocate_floating_ip(context, project_id,
                True)
            LOG.debug(_("floating IP allocation for instance "
                        "|%(floating_address)s|") % locals(),
                        instance_uuid=instance_uuid, context=context)
            # set auto_assigned column to true for the floating ip
            self.db.floating_ip_set_auto_assigned(context, floating_address)

            # get the first fixed address belonging to the instance
            fixed_ips = nw_info.fixed_ips()
            fixed_address = fixed_ips[0]['address']

            # associate the floating ip to fixed_ip
            self.associate_floating_ip(context,
                                       floating_address,
                                       fixed_address,
                                       affect_auto_assigned=True)

            # create a fresh set of network info that contains the floating ip
            nw_info = self.get_instance_nw_info(context, **kwargs)

        return nw_info

    def deallocate_for_instance(self, context, **kwargs):
        """Handles deallocating floating IP resources for an instance.

        calls super class deallocate_for_instance() as well.

        rpc.called by network_api
        """
        instance_uuid = kwargs.get('instance_id')

        if not uuidutils.is_uuid_like(instance_uuid):
            # NOTE(francois.charlier): in some cases the instance might be
            # deleted before the IPs are released, so we need to get deleted
            # instances too
            instance = self.db.instance_get(
                    context.elevated(read_deleted='yes'), instance_uuid)
            instance_uuid = instance['uuid']

        try:
            fixed_ips = self.db.fixed_ip_get_by_instance(context,
                                                         instance_uuid)
        except exception.FixedIpNotFoundForInstance:
            fixed_ips = []
        # add to kwargs so we can pass to super to save a db lookup there
        kwargs['fixed_ips'] = fixed_ips
        for fixed_ip in fixed_ips:
            fixed_id = fixed_ip['id']
            floating_ips = self.db.floating_ip_get_by_fixed_ip_id(context,
                                                                  fixed_id)
            # disassociate floating ips related to fixed_ip
            for floating_ip in floating_ips:
                address = floating_ip['address']
                try:
                    self.disassociate_floating_ip(context,
                                                  address,
                                                  affect_auto_assigned=True)
                except exception.FloatingIpNotAssociated:
                    LOG.exception(_("Floating IP is not associated. Ignore."))
                # deallocate if auto_assigned
                if floating_ip['auto_assigned']:
                    self.deallocate_floating_ip(context, address,
                                                affect_auto_assigned=True)

        # call the next inherited class's deallocate_for_instance()
        # which is currently the NetworkManager version
        # call this after so floating IPs are handled first
        super(FloatingIP, self).deallocate_for_instance(context, **kwargs)

    def _floating_ip_owned_by_project(self, context, floating_ip):
        """Raises if floating ip does not belong to project."""
        if context.is_admin:
            return

        if floating_ip['project_id'] != context.project_id:
            if floating_ip['project_id'] is None:
                LOG.warn(_('Address |%(address)s| is not allocated'),
                           {'address': floating_ip['address']})
                raise exception.NotAuthorized()
            else:
                LOG.warn(_('Address |%(address)s| is not allocated to your '
                           'project |%(project)s|'),
                           {'address': floating_ip['address'],
                           'project': context.project_id})
                raise exception.NotAuthorized()

    def allocate_floating_ip(self, context, project_id, auto_assigned=False,
                             pool=None):
        """Gets a floating ip from the pool."""
        # NOTE(tr3buchet): all network hosts in zone now use the same pool
        pool = pool or CONF.default_floating_pool
        use_quota = not auto_assigned

        # Check the quota; can't put this in the API because we get
        # called into from other places
        try:
            if use_quota:
                reservations = QUOTAS.reserve(context, floating_ips=1)
        except exception.OverQuota:
            pid = context.project_id
            LOG.warn(_("Quota exceeded for %(pid)s, tried to allocate "
                       "floating IP") % locals())
            raise exception.FloatingIpLimitExceeded()

        try:
            floating_ip = self.db.floating_ip_allocate_address(context,
                                                               project_id,
                                                               pool)
            payload = dict(project_id=project_id, floating_ip=floating_ip)
            notifier.notify(context,
                            notifier.publisher_id("network"),
                            'network.floating_ip.allocate',
                            notifier.INFO, payload)

            # Commit the reservations
            if use_quota:
                QUOTAS.commit(context, reservations)
        except Exception:
            with excutils.save_and_reraise_exception():
                if use_quota:
                    QUOTAS.rollback(context, reservations)

        return floating_ip

    @rpc_common.client_exceptions(exception.FloatingIpNotFoundForAddress)
    def deallocate_floating_ip(self, context, address,
                               affect_auto_assigned=False):
        """Returns a floating ip to the pool."""
        floating_ip = self.db.floating_ip_get_by_address(context, address)

        # handle auto_assigned
        if not affect_auto_assigned and floating_ip.get('auto_assigned'):
            return
        use_quota = not floating_ip.get('auto_assigned')

        # make sure project owns this floating ip (allocated)
        self._floating_ip_owned_by_project(context, floating_ip)

        # make sure floating ip is not associated
        if floating_ip['fixed_ip_id']:
            floating_address = floating_ip['address']
            raise exception.FloatingIpAssociated(address=floating_address)

        # clean up any associated DNS entries
        self._delete_all_entries_for_ip(context,
                                       floating_ip['address'])
        payload = dict(project_id=floating_ip['project_id'],
                       floating_ip=floating_ip['address'])
        notifier.notify(context,
                        notifier.publisher_id("network"),
                        'network.floating_ip.deallocate',
                        notifier.INFO, payload=payload)

        # Get reservations...
        try:
            if use_quota:
                reservations = QUOTAS.reserve(context, floating_ips=-1)
            else:
                reservations = None
        except Exception:
            reservations = None
            LOG.exception(_("Failed to update usages deallocating "
                            "floating IP"))

        self.db.floating_ip_deallocate(context, address)

        # Commit the reservations
        if reservations:
            QUOTAS.commit(context, reservations)

    @rpc_common.client_exceptions(exception.FloatingIpNotFoundForAddress)
    def associate_floating_ip(self, context, floating_address, fixed_address,
                              affect_auto_assigned=False):
        """Associates a floating ip with a fixed ip.

        Makes sure everything makes sense then calls _associate_floating_ip,
        rpc'ing to correct host if i'm not it.

        Access to the floating_address is verified but access to the
        fixed_address is not verified. This assumes that that the calling
        side has already verified that the fixed_address is legal by
        checking access to the instance.
        """
        floating_ip = self.db.floating_ip_get_by_address(context,
                                                         floating_address)
        # handle auto_assigned
        if not affect_auto_assigned and floating_ip.get('auto_assigned'):
            return

        # make sure project owns this floating ip (allocated)
        self._floating_ip_owned_by_project(context, floating_ip)

        # disassociate any already associated
        orig_instance_uuid = None
        if floating_ip['fixed_ip_id']:
            # find previously associated instance
            fixed_ip = self.db.fixed_ip_get(context,
                                            floating_ip['fixed_ip_id'])
            if fixed_ip['address'] == fixed_address:
                # NOTE(vish): already associated to this address
                return
            orig_instance_uuid = fixed_ip['instance_uuid']

            self.disassociate_floating_ip(context, floating_address)

        fixed_ip = self.db.fixed_ip_get_by_address(context, fixed_address)

        # send to correct host, unless i'm the correct host
        network = self.db.network_get(context.elevated(),
                                      fixed_ip['network_id'])
        if network['multi_host']:
            instance = self.db.instance_get_by_uuid(context,
                                                    fixed_ip['instance_uuid'])
            host = instance['host']
        else:
            host = network['host']

        interface = floating_ip.get('interface')
        if host == self.host:
            # i'm the correct host
            self._associate_floating_ip(context, floating_address,
                                        fixed_address, interface,
                                        fixed_ip['instance_uuid'])
        else:
            # send to correct host
            self.network_rpcapi._associate_floating_ip(context,
                    floating_address, fixed_address, interface, host,
                    fixed_ip['instance_uuid'])

        return orig_instance_uuid

    def _associate_floating_ip(self, context, floating_address, fixed_address,
                               interface, instance_uuid):
        """Performs db and driver calls to associate floating ip & fixed ip."""
        interface = CONF.public_interface or interface

        @utils.synchronized(unicode(floating_address))
        def do_associate():
            # associate floating ip
            fixed = self.db.floating_ip_fixed_ip_associate(context,
                                                           floating_address,
                                                           fixed_address,
                                                           self.host)
            if not fixed:
                # NOTE(vish): ip was already associated
                return
            try:
                # gogo driver time
                self.l3driver.add_floating_ip(floating_address, fixed_address,
                        interface, fixed['network'])
            except processutils.ProcessExecutionError as e:
                self.db.floating_ip_disassociate(context, floating_address)
                if "Cannot find device" in str(e):
                    LOG.error(_('Interface %(interface)s not found'), locals())
                    raise exception.NoFloatingIpInterface(interface=interface)
                raise

            payload = dict(project_id=context.project_id,
                           instance_id=instance_uuid,
                           floating_ip=floating_address)
            notifier.notify(context,
                            notifier.publisher_id("network"),
                            'network.floating_ip.associate',
                        notifier.INFO, payload=payload)
        do_associate()

    @rpc_common.client_exceptions(exception.FloatingIpNotFoundForAddress)
    def disassociate_floating_ip(self, context, address,
                                 affect_auto_assigned=False):
        """Disassociates a floating ip from its fixed ip.

        Makes sure everything makes sense then calls _disassociate_floating_ip,
        rpc'ing to correct host if i'm not it.
        """
        floating_ip = self.db.floating_ip_get_by_address(context, address)

        # handle auto assigned
        if not affect_auto_assigned and floating_ip.get('auto_assigned'):
            raise exception.CannotDisassociateAutoAssignedFloatingIP()

        # make sure project owns this floating ip (allocated)
        self._floating_ip_owned_by_project(context, floating_ip)

        # make sure floating ip is associated
        if not floating_ip.get('fixed_ip_id'):
            floating_address = floating_ip['address']
            raise exception.FloatingIpNotAssociated(address=floating_address)

        fixed_ip = self.db.fixed_ip_get(context, floating_ip['fixed_ip_id'])

        # send to correct host, unless i'm the correct host
        network = self.db.network_get(context.elevated(),
                                      fixed_ip['network_id'])
        interface = floating_ip.get('interface')
        if network['multi_host']:
            instance = self.db.instance_get_by_uuid(context,
                                                    fixed_ip['instance_uuid'])
            service = self.db.service_get_by_host_and_topic(
                    context.elevated(), instance['host'], CONF.network_topic)
            if service and self.servicegroup_api.service_is_up(service):
                host = instance['host']
            else:
                # NOTE(vish): if the service is down just deallocate the data
                #             locally. Set the host to local so the call will
                #             not go over rpc and set interface to None so the
                #             teardown in the driver does not happen.
                host = self.host
                interface = None
        else:
            host = network['host']

        if host == self.host:
            # i'm the correct host
            self._disassociate_floating_ip(context, address, interface,
                                           fixed_ip['instance_uuid'])
        else:
            # send to correct host
            self.network_rpcapi._disassociate_floating_ip(context, address,
                    interface, host, fixed_ip['instance_uuid'])

    def _disassociate_floating_ip(self, context, address, interface,
                                  instance_uuid):
        """Performs db and driver calls to disassociate floating ip."""
        interface = CONF.public_interface or interface

        @utils.synchronized(unicode(address))
        def do_disassociate():
            # NOTE(vish): Note that we are disassociating in the db before we
            #             actually remove the ip address on the host. We are
            #             safe from races on this host due to the decorator,
            #             but another host might grab the ip right away. We
            #             don't worry about this case because the minuscule
            #             window where the ip is on both hosts shouldn't cause
            #             any problems.
            fixed = self.db.floating_ip_disassociate(context, address)

            if not fixed:
                # NOTE(vish): ip was already disassociated
                return
            if interface:
                # go go driver time
                self.l3driver.remove_floating_ip(address, fixed['address'],
                                                 interface, fixed['network'])
            payload = dict(project_id=context.project_id,
                           instance_id=instance_uuid,
                           floating_ip=address)
            notifier.notify(context,
                            notifier.publisher_id("network"),
                            'network.floating_ip.disassociate',
                            notifier.INFO, payload=payload)
        do_disassociate()

    @rpc_common.client_exceptions(exception.FloatingIpNotFound)
    def get_floating_ip(self, context, id):
        """Returns a floating IP as a dict."""
        # NOTE(vish): This is no longer used but can't be removed until
        #             we major version the network_rpcapi.
        return dict(self.db.floating_ip_get(context, id).iteritems())

    def get_floating_pools(self, context):
        """Returns list of floating pools."""
        # NOTE(maurosr) This method should be removed in future, replaced by
        # get_floating_ip_pools. See bug #1091668
        return self.get_floating_ip_pools(context)

    def get_floating_ip_pools(self, context):
        """Returns list of floating ip pools."""
        # NOTE(vish): This is no longer used but can't be removed until
        #             we major version the network_rpcapi.
        pools = self.db.floating_ip_get_pools(context)
        return [dict(pool.iteritems()) for pool in pools]

    def get_floating_ip_by_address(self, context, address):
        """Returns a floating IP as a dict."""
        # NOTE(vish): This is no longer used but can't be removed until
        #             we major version the network_rpcapi.
        return dict(self.db.floating_ip_get_by_address(context,
                                                       address).iteritems())

    def get_floating_ips_by_project(self, context):
        """Returns the floating IPs allocated to a project."""
        # NOTE(vish): This is no longer used but can't be removed until
        #             we major version the network_rpcapi.
        ips = self.db.floating_ip_get_all_by_project(context,
                                                     context.project_id)
        return [dict(ip.iteritems()) for ip in ips]

    def get_floating_ips_by_fixed_address(self, context, fixed_address):
        """Returns the floating IPs associated with a fixed_address."""
        # NOTE(vish): This is no longer used but can't be removed until
        #             we major version the network_rpcapi.
        floating_ips = self.db.floating_ip_get_by_fixed_address(context,
                                                                fixed_address)
        return [floating_ip['address'] for floating_ip in floating_ips]

    def _is_stale_floating_ip_address(self, context, floating_ip):
        try:
            self._floating_ip_owned_by_project(context, floating_ip)
        except exception.NotAuthorized:
            return True
        return False if floating_ip.get('fixed_ip_id') else True

    def migrate_instance_start(self, context, instance_uuid,
                               floating_addresses,
                               rxtx_factor=None, project_id=None,
                               source=None, dest=None):
        # We only care if floating_addresses are provided and we're
        # switching hosts
        if not floating_addresses or (source and source == dest):
            return

        LOG.info(_("Starting migration network for instance"
                   " %(instance_uuid)s"), locals())
        for address in floating_addresses:
            floating_ip = self.db.floating_ip_get_by_address(context,
                                                             address)

            if self._is_stale_floating_ip_address(context, floating_ip):
                LOG.warn(_("Floating ip address |%(address)s| no longer "
                           "belongs to instance %(instance_uuid)s. Will not"
                           "migrate it "), locals())
                continue

            interface = CONF.public_interface or floating_ip['interface']
            fixed_ip = self.db.fixed_ip_get(context,
                                            floating_ip['fixed_ip_id'],
                                            get_network=True)
            self.l3driver.remove_floating_ip(floating_ip['address'],
                                             fixed_ip['address'],
                                             interface,
                                             fixed_ip['network'])

            # NOTE(ivoks): Destroy conntrack entries on source compute
            # host.
            self.l3driver.clean_conntrack(fixed_ip['address'])

            # NOTE(wenjianhn): Make this address will not be bound to public
            # interface when restarts nova-network on dest compute node
            self.db.floating_ip_update(context,
                                       floating_ip['address'],
                                       {'host': None})

    def migrate_instance_finish(self, context, instance_uuid,
                                floating_addresses, host=None,
                                rxtx_factor=None, project_id=None,
                                source=None, dest=None):
        # We only care if floating_addresses are provided and we're
        # switching hosts
        if host and not dest:
            dest = host
        if not floating_addresses or (source and source == dest):
            return

        LOG.info(_("Finishing migration network for instance"
                   " %(instance_uuid)s"), locals())

        for address in floating_addresses:
            floating_ip = self.db.floating_ip_get_by_address(context,
                                                             address)

            if self._is_stale_floating_ip_address(context, floating_ip):
                LOG.warn(_("Floating ip address |%(address)s| no longer "
                           "belongs to instance %(instance_uuid)s. Will not"
                           "setup it."), locals())
                continue

            self.db.floating_ip_update(context,
                                       floating_ip['address'],
                                       {'host': dest})

            interface = CONF.public_interface or floating_ip['interface']
            fixed_ip = self.db.fixed_ip_get(context,
                                            floating_ip['fixed_ip_id'],
                                            get_network=True)
            self.l3driver.add_floating_ip(floating_ip['address'],
                                          fixed_ip['address'],
                                          interface,
                                          fixed_ip['network'])

    def _prepare_domain_entry(self, context, domain):
        domainref = self.db.dnsdomain_get(context, domain)
        scope = domainref['scope']
        if scope == 'private':
            av_zone = domainref['availability_zone']
            this_domain = {'domain': domain,
                         'scope': scope,
                         'availability_zone': av_zone}
        else:
            project = domainref['project_id']
            this_domain = {'domain': domain,
                         'scope': scope,
                         'project': project}
        return this_domain

    def get_dns_domains(self, context):
        domains = []

        db_domain_list = self.db.dnsdomain_list(context)
        floating_driver_domain_list = self.floating_dns_manager.get_domains()
        instance_driver_domain_list = self.instance_dns_manager.get_domains()

        for db_domain in db_domain_list:
            if (db_domain in floating_driver_domain_list or
                    db_domain in instance_driver_domain_list):
                    domain_entry = self._prepare_domain_entry(context,
                                                              db_domain)
                    if domain_entry:
                        domains.append(domain_entry)
            else:
                LOG.warn(_('Database inconsistency: DNS domain |%s| is '
                         'registered in the Nova db but not visible to '
                         'either the floating or instance DNS driver. It '
                         'will be ignored.'), db_domain)

        return domains

    def add_dns_entry(self, context, address, name, dns_type, domain):
        self.floating_dns_manager.create_entry(name, address,
                                               dns_type, domain)

    def modify_dns_entry(self, context, address, name, domain):
        self.floating_dns_manager.modify_address(name, address,
                                                 domain)

    def delete_dns_entry(self, context, name, domain):
        self.floating_dns_manager.delete_entry(name, domain)

    def _delete_all_entries_for_ip(self, context, address):
        domain_list = self.get_dns_domains(context)
        for domain in domain_list:
            names = self.get_dns_entries_by_address(context,
                                                    address,
                                                    domain['domain'])
            for name in names:
                self.delete_dns_entry(context, name, domain['domain'])

    def get_dns_entries_by_address(self, context, address, domain):
        return self.floating_dns_manager.get_entries_by_address(address,
                                                                domain)

    def get_dns_entries_by_name(self, context, name, domain):
        return self.floating_dns_manager.get_entries_by_name(name,
                                                             domain)

    def create_private_dns_domain(self, context, domain, av_zone):
        self.db.dnsdomain_register_for_zone(context, domain, av_zone)
        try:
            self.instance_dns_manager.create_domain(domain)
        except exception.FloatingIpDNSExists:
            LOG.warn(_('Domain |%(domain)s| already exists, '
                       'changing zone to |%(av_zone)s|.'),
                     {'domain': domain, 'av_zone': av_zone})

    def create_public_dns_domain(self, context, domain, project):
        self.db.dnsdomain_register_for_project(context, domain, project)
        try:
            self.floating_dns_manager.create_domain(domain)
        except exception.FloatingIpDNSExists:
            LOG.warn(_('Domain |%(domain)s| already exists, '
                       'changing project to |%(project)s|.'),
                     {'domain': domain, 'project': project})

    def delete_dns_domain(self, context, domain):
        self.db.dnsdomain_unregister(context, domain)
        self.floating_dns_manager.delete_domain(domain)

    def _get_project_for_domain(self, context, domain):
        return self.db.dnsdomain_project(context, domain)


class LocalManager(base.Base, FloatingIP):
    def __init__(self):
        super(LocalManager, self).__init__()
        # NOTE(vish): setting the host to none ensures that the actual
        #             l3driver commands for l3 are done via rpc.
        self.host = None
        self.servicegroup_api = servicegroup.API()
        self.network_rpcapi = network_rpcapi.NetworkAPI()
        self.floating_dns_manager = importutils.import_object(
                CONF.floating_ip_dns_manager)
        self.instance_dns_manager = importutils.import_object(
                CONF.instance_dns_manager)
