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
    set_new
import vlcp.service.kvdb.objectdb as objectdb
from vlcp.event.runnable import RoutineContainer
from namedstruct.stdprim import create_binary, uint64
from vlcp.utils.ethernet import mac_addr_bytes
from random import randint
from vlcp.utils.networkmodel import LogicalPort
import ast

class DockerInfo(DataObject):
    _prefix = 'viperflow.dockerplugin.portinfo'
    _indices = ("id",)

LogicalPort._register_auto_remove('DockerInfo', lambda x: [DockerInfo.default_key(x.id)])

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
                params = json.loads(_str(self.data, charset), charset)
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

class NetworkPlugin(HttpHandler):
    def __init__(self, parent):
        HttpHandler.__init__(self, parent.scheduler, False, parent.vhostbind)
        self._parent = parent
        self._logger = parent._logger
        self._macbase = uint64.create(create_binary(mac_addr_bytes(self._parent.mactemplate), 8))
    @HttpHandler.route(b'/Plugin.Activate', method = [b'POST'])
    def plugin_activate(self, env):
        env.outputjson({"Implements": ["NetworkDriver"]})
    @HttpHandler.route(b'/NetworkDriver.GetCapabilities', method = [b'POST'])
    def getcapabilities(self, env):
        env.outputjson({"Scope": "global"})
    @_routeapi(b'/NetworkDriver.CreateNetwork')
    def createnetwork(self, env, params):
        lognet_id = 'docker-' + params['NetworkID'] + '-lognet'
        network_params = {'id': lognet_id}
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
        for m in callAPI(self, 'viperflow', 'createlogicalnetwork', network_params):
            yield m
        subnet_params = {'logicalnetwork': lognet_id,
                        'cidr': params['IPv4Data'][0]['Pool'],
                        'id': 'docker-' + params['NetworkID'] + '-subnet'}
        if params['IPv4Data'] and 'Gateway' in params['IPv4Data'][0]:
            gateway, _, _ = params['IPv4Data'][0]['Gateway'].rpartition('/')
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
        try:
            for m in callAPI(self, 'viperflow', 'createsubnet', subnet_params):
                yield m
        except Exception as exc:
            try:
                for m in callAPI(self, 'viperflow', 'deletelogicalnetwork', {'id': lognet_id}):
                    yield m
            except Exception:
                pass
            raise exc
        env.outputjson({})
    @_routeapi(b'/NetworkDriver.DeleteNetwork')
    def deletenetwork(self, env, params):
        for m in callAPI(self, 'viperflow', 'deletesubnet', {'id': 'docker-' + params['NetworkID'] + '-subnet'}):
            yield m
        for m in callAPI(self, 'viperflow', 'deletelogicalnetwork', {'id': 'docker-' + params['NetworkID'] + '-lognet'}):
            yield m
    @_routeapi(b'/NetworkDriver.CreateEndpoint')
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
                ip,f,prefix = interface['Address'].rpartition('/')
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
        for m in callAPI(self, 'viperflow', 'createlogicalport', logport_params):
            yield m
        ip_address = self.retvalue[0]['ip_address']
        subnet_cidr = self.retvalue[0]['subnet']['cidr']
        mtu = self.retvalue[0]['network'].get('mtu', self._parent.mtu)
        _, _, prefix = subnet_cidr.partition('/')
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
            result = {'Interface':{'MacAddress': mac_address}}
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
    @_routeapi(b'/NetworkDriver.EndpointOperInfo')
    def operinfo(self, env, params):
        env.outputjson({'Value':{}})
    @_routeapi(b'/NetworkDriver.DeleteEndpoint')
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
            _unplug_ovs(ovs_command, bridge_name, device_name)
            _delete_veth(ip_command, device_name)
        for m in self._parent.taskpool.runTask(self, _unplug_port):
            yield m
        for m in callAPI(self, 'viperflow', 'deletelogicalport', {'id': logport_id}):
            yield m
        env.outputjson({})
    @_routeapi(b'/NetworkDriver.Join')
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
    @_routeapi(b'/NetworkDriver.Leave')
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
    @_routeapi(b'/NetworkDriver.DiscoverNew')
    def discover_new(self, env, params):
        env.outputjson({})
    @_routeapi(b'/NetworkDriver.DiscoverDelete')
    def discover_delete(self, env, params):
        env.outputjson({})
    
@defaultconfig
@depend(httpserver.HttpServer, viperflow.ViperFlow, objectdb.ObjectDB)
class DockerPlugin(Module):
    _default_vhostbind = 'docker'
    _default_ovsbridge = 'dockerbr0'
    _default_vethprefix = 'vlcp'
    _default_ipcommand = 'ip'
    _default_ovscommand = 'ovs-vsctl'
    _default_dstprefix = 'eth'
    _default_mactemplate = '02:00:00:00:00:00'
    _default_mtu = 1500
    def __init__(self, server):
        Module.__init__(self, server)
        taskpool = TaskPool(self.scheduler)
        self.taskpool = taskpool
        self.routines.append(self.taskpool)
        self.routines.append(NetworkPlugin(self))
        self.apiroutine = RoutineContainer(self.scheduler)
        self._reqid = 0
        self.createAPI(api(self.getdockerinfo, self.apiroutine))
    def _dumpkeys(self, keys):
        self._reqid += 1
        reqid = ('dockerplugin',self._reqid)

        for m in callAPI(self.apiroutine,'objectdb','mget',{'keys':keys,'requestid':reqid}):
            yield m

        retobjs = self.apiroutine.retvalue

        with watch_context(keys,retobjs,reqid,self.apiroutine):
            self.apiroutine.retvalue = [dump(v) for v in retobjs]
    def getdockerinfo(self, portid):
        if not isinstance(portid, str) and hasattr(portid, '__iter__'):
            for m in self._dumpkeys([DockerInfo.default_key(p) for p in portid]):
                yield m
        else:            
            for m in self._dumpkeys([DockerInfo.default_key(portid)]):
                yield m
    