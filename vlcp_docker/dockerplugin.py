'''
Created on 2016/8/22

:author: hubo
'''
from vlcp.server.module import Module, callAPI, depend
import vlcp.service.connection.httpserver as httpserver
import vlcp.service.sdn.viperflow as viperflow
from vlcp.utils.connector import TaskPool
from vlcp.utils.http import HttpHandler
from vlcp.config import defaultconfig
from email.message import Message
import json
import functools
import re
from vlcp.utils.ethernet import ip4_addr

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

def _create_veth(ip_command, prefix, mac_address):
    last_exc = None
    for _ in range(0, 3):
        device_name = prefix + str(random.randrange(1, 1000000))
        try:
            subprocess.check_call([ip_command, "link", "add", device_name]
                                  + ["address", mac_address] if mac_address else []
                                  + ["type", "veth", "peer", "name", device_name + "-tap"])
        except Exception as exc:
            last_exc = exc
        else:
            last_exc = None
            break
    else:
        raise last_exc
    ip_output = subprocess.check_output([ip_command, "link", "show", "dev", device_name])
    m = re.search("link/ether ([0-9a-zA-Z:]+)", ip_output)
    if not m:
        raise ValueError('Cannot create interface')
    mac_address = m.group(1)
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
        network_params.update(params['Options'])
        for m in callAPI(self, 'viperflow', 'createnetwork', network_params):
            yield m
        subnet_params = {'logicalnetwork': lognet_id,
                        'cidr': params['IPv4Data']['Pool'],
                        'id': 'docker-' + params['NetworkID'] + '-subnet'}
        if 'Gateway' in params['IPv4Data']:
            subnet_params['gateway'] = params['IPv4Data']['Gateway']
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
            if 'Address' in interface:
                logport_params['ip_address'] = interface['Address']
            if 'MacAddress' in interface:
                logport_params['mac_address'] = interface['MacAddress']
                mac_address = interface['MacAddress']
        for m in callAPI(self, 'viperflow', 'createlogicalport', logport_params):
            yield m
        port_created = False
        try:
            for m in self._parent.taskpool.runTask(self, lambda: _create_veth(self._parent.ipcommand,
                                                                             self._parent.vethprefix,
                                                                             mac_address)):
                yield m
            port_created = True
            device_name, mac_address2 = self.retvalue
            update_params = {'id': logport_id, 'docker_port': device_name}
            if not mac_address:
                mac_address = mac_address2
                update_params['mac_address'] = mac_address2
            for m in callAPI(self, 'viperflow', 'updatelogicalport', update_params):
                yield m
            ip_address = self.retvalue[0]['ip_address']
            for m in self._parent.taskpool.runTask(self, lambda: _plug_ovs(self._parent.ovscommand,
                                                                          self._parent.ovsbridge,
                                                                          device_name,
                                                                          logport_id)):
                yield m
            env.outputjson({'Interface':{'MacAddress': mac_address, 'Address': ip_address}})
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
        for m in callAPI(self, 'viperflow', 'listlogicalports', {'id': logport_id}):
            yield m
        if not self.retvalue:
            raise KeyError(repr(params['EndpointID']) + ' not found')
        logport_result = self.retvalue[0]
        docker_port = logport_result['docker_port']
        def _unplug_port(ovs_command = self.parent.ovscommand,
                         bridge_name = self.parent.ovsbridge,
                         device_name = docker_port,
                         ip_command = self.parent.ipcommand):
            _unplug_ovs(ovs_command, bridge_name, device_name)
            _delete_veth(ip_command, device_name)
        for m in self.parent.taskpool.runTask(self, _unplug_port):
            yield m
        env.outputjson({})
    @_routeapi(b'/NetworkDriver.Join')
    def endpoint_join(self, env, params):
        logport_id = 'docker-' + params['EndpointID']
        for m in callAPI(self, 'viperflow', 'listlogicalports', {'id': logport_id}):
            yield m
        if not self.retvalue:
            raise KeyError(repr(params['EndpointID']) + ' not found')
        logport_result = self.retvalue[0]
        docker_port = logport_result['docker_port']
        result = {'InterfaceName': {'SrcName': docker_port,
                                    'DstPrefix': self.parent.dstprefix}}
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
        for m in callAPI(self, 'viperflow', 'updatelogicalport', {'id': logport_id,
                                                                  'docker_sandbox': params['SandboxKey']}):
            yield m
        env.outputjson(result)
    @_routeapi(b'/NetworkDriver.Leave')
    def endpoint_leave(self, env, params):
        logport_id = 'docker-' + params['EndpointID']
        for m in callAPI(self, 'viperflow', 'updatelogicalport', {'id': logport_id,
                                                                  'docker_sandbox': None}):
            yield m
        env.outputjson({})
    @_routeapi(b'/NetworkDriver.DiscoverNew')
    def discover_new(self, env, params):
        env.outputjson({})
    @_routeapi(b'/NetworkDriver.DiscoverDelete')
    def discover_delete(self, env, params):
        env.outputjson({})
    
@defaultconfig
@depend(httpserver.HttpServer, viperflow.ViperFlow)
class DockerPlugin(Module):
    _default_vhostbind = 'docker'
    _default_ovsbridge = 'dockerbr0'
    _default_vethprefix = 'vlcp'
    _default_ipcommand = 'ip'
    _default_ovscommand = 'ovs-vsctl'
    _default_dstprefix = 'eth'
    def __init__(self, server):
        Module.__init__(self, server)
        taskpool = TaskPool(self.scheduler)
        self.taskpool = taskpool
        self.routines.append(self.taskpool)
        self.routines.append(NetworkPlugin(self))
    