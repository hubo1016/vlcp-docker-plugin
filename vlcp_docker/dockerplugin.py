'''
Created on 2016/8/22

:author: hubo
'''
from vlcp.server.module import Module, callAPI, depend, api
import vlcp.service.connection.httpserver as httpserver
import vlcp.service.sdn.viperflow as viperflow
from vlcp.utils.connector import TaskPool
from vlcp.utils.http import HttpHandler
from vlcp.config import defaultconfig
from email.message import Message
import json
import functools
import re
from vlcp.utils.ethernet import ip4_addr, mac_addr
from vlcp.utils.dataobject import DataObject, watch_context, dump, updater,\
    set_new, ReferenceObject
import vlcp.service.kvdb.objectdb as objectdb
from vlcp.event.runnable import RoutineContainer
from namedstruct.stdprim import create_binary, uint64
from vlcp.utils.ethernet import mac_addr_bytes
from random import randint, expovariate
from vlcp.utils.networkmodel import LogicalPort, SubNet, SubNetMap
from vlcp.utils.netutils import parse_ip4_network, network_first, network_last,\
    ip_in_network
from uuid import uuid1
import ast
from random import random
from vlcp.event.lock import Lock

class DockerInfo(DataObject):
    _prefix = 'viperflow.dockerplugin.portinfo'
    _indices = ("id",)

LogicalPort._register_auto_remove('DockerInfo', lambda x: [DockerInfo.default_key(x.id)])

class IPAMReserve(DataObject):
    _prefix = 'viperflow.dockerplugin.ipamreserve'
    _indices = ("id",)
    def __init__(self, prefix=None, deleted=False):
        DataObject.__init__(self, prefix=prefix, deleted=deleted)
        # Should be dictionary of {IP, timeout}
        self.reserved_ips = {}

SubNet._register_auto_remove('IPAMReserve', lambda x: [IPAMReserve.default_key(x.docker_ipam_poolid)] \
                                                        if hasattr(x, 'docker_ipam_poolid') else [])

class IPAMPoolReserve(DataObject):
    _prefix = 'viperflow.dockerplugin.ipampool'
    def __init__(self, prefix=None, deleted=False):
        DataObject.__init__(self, prefix=prefix, deleted=deleted)
        # Should be dictionary of {PoolId: [CIDR, timeout]}
        self.reserved_pools = {}
        self.nextalloc = 0

class IPAMReserveMarker(DataObject):
    _prefix = 'viperflow.dockerplugin.ipamreservemarker'
    _indices = ("cidr",)

def _str(b, encoding = 'ascii'):
    if isinstance(b, str):
        return b
    else:
        return b.decode(encoding)

def _routeapi(path):
    def decorator(func):
        @HttpHandler.route(path, method = [b'POST'])
        @functools.wraps(func)
        def handler(self, env):
            try:
                if b'content-type' in env.headerdict:
                    m = Message()
                    m['content-type'] = _str(env.headerdict[b'content-type'])
                    charset = m.get_content_charset('utf-8')
                else:
                    charset = 'utf-8'
                for m in env.inputstream.read(self):
                    yield m
                params = json.loads(_str(self.data, charset), encoding=charset)
                self._logger.debug('Call %r with parameters: %r', path, params)
                r = func(self, env, params)
                if r is not None:
                    for m in r:
                        yield m
            except Exception as exc:
                self._logger.warning('Docker API failed with exception', exc_info = True)
                env.startResponse(500)
                env.outputjson({'Err': str(exc)})
        return handler
    return decorator

import subprocess
import random

def _create_veth(ip_command, prefix, mac_address, mtu):
    last_exc = None
    for _ in range(0, 3):
        device_name = prefix + str(random.randrange(1, 1000000))
        try:
            subprocess.check_call([ip_command, "link", "add", device_name]
                                  + (["address", mac_address] if mac_address else [])
                                  + (["mtu", str(mtu)] if mtu is not None else [])
                                  + ["type", "veth", "peer", "name", device_name + "-tap"]
                                  + (["mtu", str(mtu)] if mtu is not None else []))
        except Exception as exc:
            last_exc = exc
        else:
            last_exc = None
            break
    else:
        raise last_exc
    ip_output = subprocess.check_output([ip_command, "link", "show", "dev", device_name])
    m = re.search(b"link/ether ([0-9a-zA-Z:]+)", ip_output)
    if not m:
        raise ValueError('Cannot create interface')
    mac_address = m.group(1)
    subprocess.check_call([ip_command, "link", "set", device_name + "-tap", "up"])
    return (device_name, mac_address)

def _delete_veth(ip_command, device_name):
    subprocess.check_call([ip_command, "link", "del", device_name + "-tap"])

def _plug_ovs(ovs_command, bridge_name, device_name, port_id):
    subprocess.check_call([ovs_command, "add-port", bridge_name, device_name+"-tap", "--",
                           "set", "interface", device_name+"-tap", "external_ids:iface-id=" + port_id])

def _unplug_ovs(ovs_command, bridge_name, device_name):
    subprocess.check_call([ovs_command, "del-port", device_name+"-tap"])


class IPAMUsingException(Exception):
    pass

class RetryUpdateException(Exception):
    pass

_GLOBAL_SPACE = 'VLCPGlobalAddressSpace'

class NetworkPlugin(HttpHandler):
    def __init__(self, parent):
        HttpHandler.__init__(self, parent.scheduler, False, parent.vhostbind)
        self._parent = parent
        self._logger = parent._logger
        self._macbase = uint64.create(create_binary(mac_addr_bytes(self._parent.mactemplate), 8))
        cidrrange = parent.cidrrange
        try:
            subnet, mask = parse_ip4_network(cidrrange)
            if not (0 <= mask <= 24):
                raise ValueError
        except Exception:
            self._logger.warning('Invalid CIDR range: %r. Using default 10.0.0.0/8', cidrrange)
            subnet = ip4_addr('10.0.0.0')
            mask = 8
        self.cidrrange_subnet = subnet
        self.cidrrange_mask = mask
        self.cidrrange_end = (1 << (24 - mask))
        self.pooltimeout = parent.pooltimeout
        self.iptimeout = parent.iptimeout
        self._reqid = 0
        
    @HttpHandler.route(br'/Plugin\.Activate', method = [b'POST'])
    def plugin_activate(self, env):
        env.outputjson({"Implements": ["NetworkDriver", "IpamDriver"]})
        
    @HttpHandler.route(br'/IpamDriver\.GetDefaultAddressSpaces', method = [b'POST'])
    def ipam_addressspace(self, env):
        env.outputjson({'LocalDefaultAddressSpace': 'VLCPLocalAddressSpace',
                        'GlobalDefaultAddressSpace': _GLOBAL_SPACE})
        
    @HttpHandler.route(br'/IpamDriver\.GetCapabilities', method = [b'POST'])
    def ipam_capabilities(self, env):
        env.outputjson({"RequiresMACAddress": False,
                        "RequiresRequestReplay": False})

    def _remove_staled_pools(self, reservepool, timestamp):
        timeouts = dict((poolid, cidr) for poolid, (cidr, timeout) in reservepool.reserved_pools.items()
                        if timeout is not None and timeout < timestamp)
        for k in timeouts:
            del reservepool.reserved_pools[k]
        removed_keys = [r for poolid, cidr in timeouts.items()
                        for r in (IPAMReserve.default_key(poolid), IPAMReserveMarker.default_key(cidr))]
        return removed_keys
    
    @_routeapi(br'/IpamDriver\.RequestPool')
    def ipam_requestpool(self, env, params):
        if params['AddressSpace'] != _GLOBAL_SPACE:
            raise ValueError('Unsupported address space: must use this IPAM driver together with network driver')
        if params['V6']:
            raise ValueError('IPv6 is not supported')
        new_pool = IPAMReserve.create_instance(uuid1().hex)
        new_pool.pool = params['Pool']
        if new_pool.pool:
            subnet, mask = parse_ip4_network(new_pool.pool)
            new_pool.pool = ip4_addr.formatter(subnet) + '/' + str(mask)
        new_pool.subpool = params['SubPool']
        if new_pool.subpool:
            subnet, mask = parse_ip4_network(new_pool.subpool)
            new_pool.subpool = ip4_addr.formatter(subnet) + '/' + str(mask)
        new_pool.options = params['Options']
        if new_pool.pool:
            l = Lock(('dockerplugin_ipam_request_pool', new_pool.pool), self.scheduler)
            for m in l.lock(self):
                yield m
        else:
            l = None
        try:
            while True:
                fail = 0
                rets = []
                def _updater(keys, values, timestamp):
                    reservepool = values[0]
                    reserve_new_pool = set_new(values[1], new_pool)
                    remove_keys = self._remove_staled_pools(reservepool, timestamp)
                    used_cidrs = set(cidr for _, (cidr, _) in reservepool.reserved_pools.items())
                    if not reserve_new_pool.pool:
                        # pool is not specified
                        for _ in range(0, self.cidrrange_end):
                            reservepool.nextalloc += 1
                            if reservepool.nextalloc >= self.cidrrange_end:
                                reservepool.nextalloc = 0
                            new_subnet = self.cidrrange_subnet | (reservepool.nextalloc << 8)
                            new_cidr = ip4_addr.formatter(new_subnet) + '/24'
                            if new_cidr not in used_cidrs:
                                break
                        reserve_new_pool.pool = new_cidr
                        reserve_new_pool.subpool = ''
                    rets[:] = [reserve_new_pool.pool]
                    if reserve_new_pool.pool in used_cidrs:
                        # We must wait until this CIDR is released
                        raise IPAMUsingException
                    reservepool.reserved_pools[reserve_new_pool.id] = \
                                                       [reserve_new_pool.pool,
                                                       timestamp + self.pooltimeout * 1000000]
                    marker = IPAMReserveMarker.create_instance(reserve_new_pool.pool)
                    if marker.getkey() in remove_keys:
                        remove_keys.remove(marker.getkey())
                        return (tuple(keys[0:2]) + tuple(remove_keys),
                            (reservepool, reserve_new_pool) + (None,) * len(remove_keys))
                    else:
                        return (tuple(keys[0:2]) + (marker.getkey(),) + tuple(remove_keys),
                            (reservepool, reserve_new_pool, marker) + (None,) * len(remove_keys))
                try:
                    for m in callAPI(self, 'objectdb', 'transact', {'keys': (IPAMPoolReserve.default_key(),
                                                                             new_pool.getkey()),
                                                                    'updater': _updater,
                                                                    'withtime': True}):
                        yield m
                except IPAMUsingException:
                    # Wait for the CIDR to be released
                    self._reqid += 1
                    fail += 1
                    reqid = ('dockerplugin_ipam', self._reqid)
                    marker_key = IPAMReserveMarker.default_key(rets[0])
                    for m in callAPI(self, 'objectdb', 'get', {'key': marker_key,
                                                               'requestid': reqid,
                                                               'nostale': True}):
                        yield m
                    retvalue = self.retvalue
                    with watch_context([marker_key], [retvalue], reqid, self):
                        if retvalue is not None and not retvalue.isdeleted():
                            for m in self.executeWithTimeout(self.pooltimeout, retvalue.waitif(self, lambda x: x.isdeleted())):
                                yield m
                else:
                    env.outputjson({'PoolID': new_pool.id,
                                    'Pool': rets[0],
                                    'Data': {}})
                    break
        finally:
            if l is not None:
                l.unlock()
            
    @_routeapi(br'/IpamDriver\.ReleasePool')
    def ipam_releasepool(self, env, params):
        poolid = params['PoolID']
        def _updater(keys, values, timestamp):
            # There are two situations for Release Pool:
            # 1. The pool is already used to create a network, in this situation, the pool should be
            #    released with the network removal
            # 2. The pool has not been used for network creation, we should release it from the reservation
            reservepool = values[0]
            removed_keys = self._remove_staled_pools(reservepool, timestamp)
            if poolid in reservepool.reserved_pools:
                removed_keys.append(IPAMReserve.default_key(poolid))
                removed_keys.append(IPAMReserveMarker.default_key(reservepool.reserved_pools[poolid][0]))
                del reservepool.reserved_pools[poolid]
            return ((keys[0],) + tuple(removed_keys), (values[0],) + (None,) * len(removed_keys))
        for m in callAPI(self, 'objectdb', 'transact', {'keys': (IPAMPoolReserve.default_key(),),
                                                        'updater': _updater,
                                                        'withtime': True}):
            yield m
        env.outputjson({})
    
    def _remove_staled_ips(self, pool, timestamp):
        pool.reserved_ips = dict((addr, ts) for addr,ts in pool.reserved_ips.items()
                                 if ts is None or ts >= timestamp)
    
    @_routeapi(br'/IpamDriver\.RequestAddress')
    def ipam_requestaddress(self, env, params):
        poolid = params['PoolID']
        address = params['Address']
        if address:
            address = ip4_addr.formatter(ip4_addr(address))
        reserve_key = IPAMReserve.default_key(poolid)
        rets = []
        def _updater(keys, values, timestamp):
            pool = values[0]
            if pool is None:
                raise ValueError('PoolID %r does not exist' % (poolid,))
            self._remove_staled_ips(pool, timestamp)
            if len(values) > 1:
                subnetmap = values[1]
                if not hasattr(pool, 'subnetmap') or pool.subnetmap.getkey() != keys[1]:
                    raise RetryUpdateException
                subnet = values[2]
            else:
                subnetmap = None
                if hasattr(pool, 'subnetmap'):
                    raise RetryUpdateException
            if address:
                # check ip_address in cidr
                if address in pool.reserved_ips:
                    raise ValueError("IP address " + address + " has been reserved")
                if pool.subpool:
                    cidr = pool.subpool
                else:
                    cidr = pool.pool
                network, mask = parse_ip4_network(cidr)
                addr_num = ip4_addr(address)
                if not ip_in_network(addr_num, network, mask):
                    raise ValueError('IP address ' + address + " is not in the network CIDR")
                if subnetmap is not None:
                    start = ip4_addr(subnet.allocated_start)
                    end = ip4_addr(subnet.allocated_end)
                    try:
                        assert start <= addr_num <= end
                        if hasattr(subnet, 'gateway'):
                            assert addr_num != ip4_addr(subnet.gateway)
                    except Exception:
                        raise ValueError("specified IP address " + address + " is not valid")
    
                    if str(addr_num) in subnetmap.allocated_ips:
                        raise ValueError("IP address " + address + " has been used")
                new_address = address
            else:
                # allocated ip_address from cidr
                gateway = None
                cidr = pool.pool
                if pool.subpool:
                    cidr = pool.subpool
                network, prefix = parse_ip4_network(cidr)
                start = network_first(network, prefix)
                end = network_last(network, prefix)
                if subnetmap is not None:
                    start = max(start, ip4_addr(subnet.allocated_start))
                    end = min(end, ip4_addr(subnet.allocated_end))
                    if hasattr(subnet, "gateway"):
                        gateway = ip4_addr(subnet.gateway)
                for ip_address in range(start,end):
                    new_address = ip4_addr.formatter(ip_address)
                    if ip_address != gateway and \
                        (subnetmap is None or str(ip_address) not in subnetmap.allocated_ips) and \
                        new_address not in pool.reserved_ips:
                        break
                else:
                    raise ValueError("No available IP address can be used")
            pool.reserved_ips[new_address] = timestamp + self.iptimeout * 1000000
            _, mask = parse_ip4_network(pool.pool)
            rets[:] = [new_address + '/' + str(mask)]
            return ((keys[0],), (pool,))
        while True:
            # First get the current data, to determine that whether the pool has already
            # been connected to a network
            self._reqid += 1
            reqid = ('dockerplugin_ipam', self._reqid)
            for m in callAPI(self, 'objectdb', 'get', {'key': reserve_key,
                                                       'requestid': reqid,
                                                       'nostale': True}):
                yield m
            reserve_pool = self.retvalue
            with watch_context([reserve_key], [reserve_pool], reqid, self):
                if reserve_pool is None or reserve_pool.isdeleted():
                    raise ValueError('PoolID %r does not exist' % (poolid,))
                if hasattr(reserve_pool, 'subnetmap'):
                    # Retrieve both the reserve pool and the subnet map
                    keys = (reserve_key, reserve_pool.subnetmap.getkey(), SubNet.default_key(reserve_pool.subnetmap.id))
                else:
                    keys = (reserve_key,)
                try:
                    for m in callAPI(self, 'objectdb', 'transact', {'keys': keys,
                                                                    'updater': _updater,
                                                                    'withtime': True}):
                        yield m
                except RetryUpdateException:
                    continue
                else:
                    env.outputjson({'Address': rets[0], 'Data': {}})
                    break
    
    @_routeapi(br'/IpamDriver\.ReleaseAddress')
    def ipam_releaseaddress(self, env, params):
        poolid = params['PoolID']
        address = params['Address']
        address = ip4_addr.formatter(ip4_addr(address))
        def _updater(keys, values, timestamp):
            pool = values[0]
            if pool is None:
                return ((), ())
            self._remove_staled_ips(pool, timestamp)
            if address in pool.reserved_ips:
                del pool.reserved_ips[address]
            return ((keys[0],), (pool,))
        for m in callAPI(self, 'objectdb', 'transact', {'keys': (IPAMReserve.default_key(poolid),),
                                                        'updater': _updater,
                                                        'withtime': True}):
            yield m
        env.outputjson({})
        
    @HttpHandler.route(br'/NetworkDriver\.GetCapabilities', method = [b'POST'])
    def getcapabilities(self, env):
        env.outputjson({"Scope": "global"})
        
    @_routeapi(br'/NetworkDriver\.CreateNetwork')
    def createnetwork(self, env, params):
        lognet_id = 'docker-' + params['NetworkID'] + '-lognet'
        subnet_id = 'docker-' + params['NetworkID'] + '-subnet'
        network_params = {'id': lognet_id}
        if params['IPv4Data'] and 'Gateway' in params['IPv4Data'][0]:
            gateway, _, _ = params['IPv4Data'][0]['Gateway'].partition('/')
        else:
            gateway = None
        if params['IPv4Data'] and params['IPv4Data'][0]['AddressSpace'] == _GLOBAL_SPACE:
            request_cidr = params['IPv4Data'][0]['Pool']
            docker_ipam_poolid = True
            def _ipam_work():
                # Using network driver together with IPAM driver
                rets = []
                def _ipam_stage(keys, values, timestamp):
                    reservepool = values[0]
                    removed_keys = self._remove_staled_pools(reservepool, timestamp)
                    poolids = [poolid for poolid, (cidr, _) in reservepool.reserved_pools.items()
                               if cidr == request_cidr]
                    if not poolids:
                        raise ValueError('Pool %r is not reserved by VLCP IPAM plugin' % (request_cidr,))
                    docker_ipam_poolid = poolids[0]
                    rets[:] = [docker_ipam_poolid]
                    removed_keys.append(IPAMReserveMarker.default_key(reservepool.reserved_pools[docker_ipam_poolid][0]))
                    del reservepool.reserved_pools[docker_ipam_poolid]
                    return ((keys[0],) + tuple(removed_keys), (reservepool,) + (None,) * len(removed_keys))
                try:
                    for m in callAPI(self, 'objectdb', 'transact', {'keys': (IPAMPoolReserve.default_key(),),
                                                                    'updater': _ipam_stage,
                                                                    'withtime': True}):
                        yield m
                    self.retvalue = (True, rets[0])
                except Exception as exc:
                    self.retvalue = (False, exc)
                    
        else:
            docker_ipam_poolid = None
        def _cleanup_ipam(docker_ipam_poolid):
            @updater
            def _remove_reserve(pool):
                return (None,)
            for m in callAPI(self, 'objectdb', 'transact', {'keys': (IPAMReserve.default_key(docker_ipam_poolid),),
                                                            'updater': _remove_reserve}):
                yield m
        if 'Options' in params and 'com.docker.network.generic' in params['Options']:
            for k,v in params['Options']['com.docker.network.generic'].items():
                if k.startswith('subnet:'):
                    pass
                elif k in ('mtu','vlanid','vni'):
                    network_params[k] = int(v)
                elif v[:1] == '`' and v[-1:] == '`':
                    try:
                        network_params[k] = ast.literal_eval(v[1:-1])
                    except Exception:
                        network_params[k] = v
                else:
                    network_params[k] = v
        def _create_lognet():
            try:
                for m in callAPI(self, 'viperflow', 'createlogicalnetwork', network_params):
                    yield m
            except Exception as exc:
                self.retvalue = exc
            else:
                self.retvalue = None
        if docker_ipam_poolid:
            for m in self.executeAll([_ipam_work(),
                                      _create_lognet()], self):
                yield m
            (((ipam_succ, ipam_result),), (create_lognet_exc,)) = self.retvalue
            if not ipam_succ:
                if create_lognet_exc is None:
                    try:
                        for m in callAPI(self, 'viperflow', 'deletelogicalnetwork', {'id': lognet_id}):
                            yield m
                    except Exception:
                        pass
                raise ipam_result
            elif create_lognet_exc is not None:
                self.subroutine(_cleanup_ipam(ipam_result))
                raise create_lognet_exc
            else:
                docker_ipam_poolid = ipam_result
            def _ipam_work2():
                def _ipam_stage2(keys, values, timestamp):
                    pool = values[0]
                    pool.subnetmap = ReferenceObject(SubNetMap.default_key(subnet_id))
                    self._remove_staled_ips(pool, timestamp)
                    if gateway is not None and gateway in pool.reserved_ips:
                        # Reserve forever
                        pool.reserved_ips[gateway] = None
                    return ((keys[0],), (pool,))
                for m in callAPI(self, 'objectdb', 'transact', {'keys': (IPAMReserve.default_key(docker_ipam_poolid),),
                                                                'updater': _ipam_stage2,
                                                                'withtime': True}):
                    yield m
        else:
            for m in _create_lognet():
                yield m
        subnet_params = {'logicalnetwork': lognet_id,
                        'cidr': params['IPv4Data'][0]['Pool'],
                        'id': subnet_id}
        if gateway is not None:
            subnet_params['gateway'] = gateway
        if 'Options' in params and 'com.docker.network.generic' in params['Options']:
            for k,v in params['Options']['com.docker.network.generic'].items():
                if k.startswith('subnet:'):
                    subnet_key = k[len('subnet:'):]
                    if subnet_key == 'disablegateway':
                        try:
                            del subnet_params['gateway']
                        except KeyError:
                            pass
                    elif v[:1] == '`' and v[-1:] == '`':
                        try:
                            subnet_params[subnet_key] = ast.literal_eval(v[1:-1])
                        except Exception:
                            subnet_params[subnet_key] = v
                    else:
                        subnet_params[subnet_key] = v
        if docker_ipam_poolid is not None:
            subnet_params['docker_ipam_poolid'] = docker_ipam_poolid
        def _create_subnet():
            for m in callAPI(self, 'viperflow', 'createsubnet', subnet_params):
                yield m
        if docker_ipam_poolid:
            routines = [_ipam_work2(), _create_subnet()]
        else:
            routines = [_create_subnet()]
        try:
            for m in self.executeAll(routines):
                yield m
        except Exception as exc:
            if docker_ipam_poolid is not None:
                self.subroutine(_cleanup_ipam(docker_ipam_poolid))
            try:
                for m in callAPI(self, 'viperflow', 'deletelogicalnetwork', {'id': lognet_id}):
                    yield m
            except Exception:
                pass
            raise exc        
        env.outputjson({})
        
    @_routeapi(br'/NetworkDriver\.DeleteNetwork')
    def deletenetwork(self, env, params):
        for m in callAPI(self, 'viperflow', 'deletesubnet', {'id': 'docker-' + params['NetworkID'] + '-subnet'}):
            yield m
        for m in callAPI(self, 'viperflow', 'deletelogicalnetwork', {'id': 'docker-' + params['NetworkID'] + '-lognet'}):
            yield m
            
    @_routeapi(br'/NetworkDriver\.CreateEndpoint')
    def createendpoint(self, env, params):
        lognet_id = 'docker-' + params['NetworkID'] + '-lognet'
        subnet_id = 'docker-' + params['NetworkID'] + '-subnet'
        logport_id = 'docker-' + params['EndpointID']
        logport_params = {}
        if 'Options' in params:
            logport_params.update(params['Options'])
        logport_params['id'] = logport_id
        logport_params['logicalnetwork'] = lognet_id
        logport_params['subnet'] = subnet_id
        mac_address = None
        if 'Interface' in params:
            interface = params['Interface']
            if 'Address' in interface and interface['Address']:
                ip,f,prefix = interface['Address'].partition('/')
                logport_params['ip_address'] = ip
            else:
                ip = None
            if 'MacAddress' in interface and interface['MacAddress']:
                logport_params['mac_address'] = interface['MacAddress']
                mac_address = interface['MacAddress']
        if mac_address is None:
            # Generate a MAC address
            if ip:
                # Generate MAC address based on IP address
                mac_num = self._macbase
                mac_num ^= ((hash(subnet_id) & 0xffffffff) << 8)
                mac_num ^= ip4_addr(ip)
            else:
                # Generate MAC address based on Port ID and random number
                mac_num = self._macbase
                mac_num ^= ((hash(logport_id) & 0xffffffff) << 8)
                mac_num ^= randint(0, 0xffffffff)
            mac_address = mac_addr_bytes.formatter(create_binary(mac_num, 6))
            logport_params['mac_address'] = mac_address
        try:
            for m in callAPI(self, 'viperflow', 'createlogicalport', logport_params):
                yield m
        except Exception as exc:
            # There is an issue that docker daemon may not delete an endpoint correctly
            # If autoremoveports is enabled, we remove the logical port automatically
            # Note that created veth and Openvswitch ports are not cleared because they
            # may not on this server, so you must clean them yourself with vlcp_docker.cleanup
            if self._parent.autoremoveports:
                for m in callAPI(self, 'viperflow', 'listlogicalports', {'logicalnetwork': lognet_id,
                                                                         'ip_address': ip}):
                    yield m
                if self.retvalue:
                    if self.retvalue[0]['id'].startswith('docker-'):
                        dup_pid = self.retvalue[0]['id']
                        self._logger.warning('Duplicated ports detected: %s (%s). Will remove it.',
                                                    dup_pid,
                                                    self.retvalue[0]['ip_address'])
                        for m in callAPI(self, 'viperflow', 'deletelogicalport', {'id': dup_pid}):
                            yield m
                        # Retry create logical port
                        for m in callAPI(self, 'viperflow', 'createlogicalport', logport_params):
                            yield m
                    else:
                        self._logger.warning('Duplicated with a non-docker port')
                        raise exc
                else:
                    raise exc
            else:
                raise exc
        ip_address = self.retvalue[0]['ip_address']
        subnet_cidr = self.retvalue[0]['subnet']['cidr']
        mtu = self.retvalue[0]['network'].get('mtu', self._parent.mtu)
        _, _, prefix = subnet_cidr.partition('/')
        if 'docker_ipam_poolid' in self.retvalue[0]['subnet']:
            docker_ipam_poolid = self.retvalue[0]['subnet']['docker_ipam_poolid']
            def _remove_ip_reservation():
                try:
                    # The reservation is completed, remove the temporary reservation
                    def _ipam_updater(keys, values, timestamp):
                        pool = values[0]
                        self._remove_staled_ips(pool, timestamp)
                        if ip_address in pool.reserved_ips:
                            del pool.reserved_ips[ip_address]
                        return ((keys[0],), (pool,))
                    for m in callAPI(self, 'objectdb', 'transact',
                                     {'keys': (IPAMReserve.default_key(docker_ipam_poolid),),
                                      'updater': _ipam_updater,
                                      'withtime': True}):
                        yield m
                except Exception:
                    self._logger.warning('Unexpected exception while removing reservation of IP address %r, will ignore and continue',
                                     ip_address, exc_info = True)
            self.subroutine(_remove_ip_reservation())
        port_created = False
        try:
            for m in self._parent.taskpool.runTask(self, lambda: _create_veth(self._parent.ipcommand,
                                                                             self._parent.vethprefix,
                                                                             mac_address,
                                                                             mtu)):
                yield m
            port_created = True
            device_name, _ = self.retvalue
            info = DockerInfo.create_instance(logport_id)
            info.docker_port = device_name
            @updater
            def _updater(dockerinfo):
                dockerinfo = set_new(dockerinfo, info)
                return (dockerinfo,)
            for m in callAPI(self, 'objectdb', 'transact', {'keys': (info.getkey(),),
                                                            'updater': _updater}):
                yield m
            for m in self._parent.taskpool.runTask(self, lambda: _plug_ovs(self._parent.ovscommand,
                                                                          self._parent.ovsbridge,
                                                                          device_name,
                                                                          logport_id)):
                yield m
            result = {'Interface': {}}
            if 'MacAddress' not in interface:
                result['Interface']['MacAddress'] = mac_address
            if 'Address' not in interface:
                result['Interface']['Address'] = ip_address + '/' + prefix
            env.outputjson(result)
        except Exception as exc:
            try:
                if port_created:
                    for m in self._parent.taskpool.runTask(self, lambda: _delete_veth(self._parent.ipcommand,
                                                                                      device_name)):
                        yield m
            except Exception:
                pass
            try:
                for m in callAPI(self, 'viperflow', 'deletelogicalport', {'id': logport_id}):
                    yield m
            except Exception:
                pass
            raise exc
        
    @_routeapi(br'/NetworkDriver\.EndpointOperInfo')
    def operinfo(self, env, params):
        env.outputjson({'Value':{}})
        
    @_routeapi(br'/NetworkDriver\.DeleteEndpoint')
    def delete_endpoint(self, env, params):
        logport_id = 'docker-' + params['EndpointID']
        for m in callAPI(self, 'dockerplugin', 'getdockerinfo', {'portid': logport_id}):
            yield m
        if not self.retvalue:
            raise KeyError(repr(params['EndpointID']) + ' not found')
        dockerinfo_result = self.retvalue[0]
        docker_port = dockerinfo_result['docker_port']
        def _unplug_port(ovs_command = self._parent.ovscommand,
                         bridge_name = self._parent.ovsbridge,
                         device_name = docker_port,
                         ip_command = self._parent.ipcommand):
            try:
                _unplug_ovs(ovs_command, bridge_name, device_name)
            except Exception:
                self._logger.warning('Remove veth from OpenvSwitch failed', exc_info = True)
            try:
                _delete_veth(ip_command, device_name)
            except Exception:
                self._logger.warning('Delete veth failed', exc_info = True)
        for m in self._parent.taskpool.runTask(self, _unplug_port):
            yield m
        for m in callAPI(self, 'viperflow', 'deletelogicalport', {'id': logport_id}):
            yield m
        env.outputjson({})
        
    @_routeapi(br'/NetworkDriver\.Join')
    def endpoint_join(self, env, params):
        logport_id = 'docker-' + params['EndpointID']
        for m in self.executeAll([callAPI(self, 'viperflow', 'listlogicalports', {'id': logport_id}),
                                  callAPI(self, 'dockerplugin', 'getdockerinfo', {'portid': logport_id})]):
            yield m
        ((logport_results,),(dockerinfo_results,)) = self.retvalue
        if not logport_results:
            raise KeyError(repr(params['EndpointID']) + ' not found')
        logport_result = logport_results[0]
        if dockerinfo_results:
            docker_port = dockerinfo_results[0]['docker_port']
        else:
            docker_port = logport_result['docker_port']
        result = {'InterfaceName': {'SrcName': docker_port,
                                    'DstPrefix': self._parent.dstprefix}}
        if 'subnet' in logport_result:
            subnet = logport_result['subnet']
            if 'gateway' in subnet:
                result['Gateway'] = subnet['gateway']
            if 'host_routes' in subnet:
                try:
                    def generate_route(r):
                        r_g = {'Destination': r[0]}
                        if ip4_addr(r[1]) == 0:
                            r_g['RouteType'] = 1
                        else:
                            r_g['RouteType'] = 0
                            r_g['NextHop'] = r[1]
                        return r_g
                    result['StaticRoutes'] = [generate_route(r)
                                              for r in subnet['host_routes']]
                except Exception:
                    self._logger.warning('Generate static routes failed', exc_info = True)
        sandboxkey = params['SandboxKey']
        @updater
        def _updater(dockerinfo):
            if dockerinfo is None:
                return ()
            else:
                dockerinfo.docker_sandbox = sandboxkey
                return (dockerinfo,)
        for m in callAPI(self, 'objectdb', 'transact', {'keys': [DockerInfo.default_key(logport_id)],
                                                        'updater': _updater}):
            yield m
        env.outputjson(result)
        
    @_routeapi(br'/NetworkDriver\.Leave')
    def endpoint_leave(self, env, params):
        logport_id = 'docker-' + params['EndpointID']
        @updater
        def _updater(dockerinfo):
            if dockerinfo is None:
                return ()
            else:
                dockerinfo.docker_sandbox = None
                return (dockerinfo,)
        for m in callAPI(self, 'objectdb', 'transact', {'keys': [DockerInfo.default_key(logport_id)],
                                                        'updater': _updater}):
            yield m
        env.outputjson({})
        
    @_routeapi(br'/NetworkDriver\.DiscoverNew')
    def discover_new(self, env, params):
        env.outputjson({})
        
    @_routeapi(br'/NetworkDriver\.DiscoverDelete')
    def discover_delete(self, env, params):
        env.outputjson({})
    
    
@defaultconfig
@depend(httpserver.HttpServer, viperflow.ViperFlow, objectdb.ObjectDB)
class DockerPlugin(Module):
    '''
    Integrate VLCP with Docker
    '''
    # Bind Docker API EndPoint (a HTTP service) to specified vHost
    _default_vhostbind = 'docker'
    # OpenvSwitch bridge used in this server
    _default_ovsbridge = 'dockerbr0'
    # Auto-created veth device prefix
    _default_vethprefix = 'vlcp'
    # Path to ``ip`` command
    _default_ipcommand = 'ip'
    # Path to ``ovs-vsctl`` command
    _default_ovscommand = 'ovs-vsctl'
    # vNIC prefix used in the docker container
    _default_dstprefix = 'eth'
    # A template MAC address used on generating MAC addresses
    _default_mactemplate = '02:00:00:00:00:00'
    # Default MTU used for networks
    _default_mtu = 1500
    # Try to remove the old port if it is not clean up correctly
    _default_autoremoveports = True
    # IPAM pool reserve timeout. If a reserved pool is not used to
    # create a network till timeout, it is automatically released.
    _default_pooltimeout = 60
    # IPAM IP reserve timeout. If an IP address is not used to
    # create an endpoint till timeout, it is automatically released.
    _default_iptimeout = 60
    # The default address space used when subnet is not specified.
    # A C-class (like 10.0.1.0/24) subnet will be assigned for each
    # network.
    _default_cidrrange = '10.0.0.0/8'
    def __init__(self, server):
        Module.__init__(self, server)
        taskpool = TaskPool(self.scheduler)
        self.taskpool = taskpool
        self.routines.append(self.taskpool)
        self.routines.append(NetworkPlugin(self))
        self.apiroutine = RoutineContainer(self.scheduler)
        self._reqid = 0
        self.createAPI(api(self.getdockerinfo, self.apiroutine))
    def load(self, container):
        @updater
        def init_ipam(poolreserve):
            if poolreserve is None:
                return (IPAMPoolReserve(),)
            else:
                return ()
        for m in callAPI(container, 'objectdb', 'transact', {'keys': (IPAMPoolReserve.default_key(),),
                                                             'updater': init_ipam}):
            yield m
        for m in Module.load(self, container):
            yield m
        
    def _dumpkeys(self, keys):
        self._reqid += 1
        reqid = ('dockerplugin',self._reqid)

        for m in callAPI(self.apiroutine,'objectdb','mget',{'keys':keys,'requestid':reqid}):
            yield m

        retobjs = self.apiroutine.retvalue

        with watch_context(keys,retobjs,reqid,self.apiroutine):
            self.apiroutine.retvalue = [dump(v) for v in retobjs]
    def getdockerinfo(self, portid):
        "Get docker info for specified port"
        if not isinstance(portid, str) and hasattr(portid, '__iter__'):
            for m in self._dumpkeys([DockerInfo.default_key(p) for p in portid]):
                yield m
        else:            
            for m in self._dumpkeys([DockerInfo.default_key(portid)]):
                yield m
    