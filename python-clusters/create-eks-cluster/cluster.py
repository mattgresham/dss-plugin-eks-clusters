import os, sys, json, subprocess, time, logging, yaml, threading

import dku_utils.tools_version
from dataiku.cluster import Cluster

from dku_aws.eksctl_command import EksctlCommand
from dku_aws.aws_command import AwsCommand
from dku_kube.kubeconfig import setup_creds_env
from dku_kube.autoscaler import add_autoscaler_if_needed
from dku_kube.gpu_driver import add_gpu_driver_if_needed
from dku_kube.metrics_server import install_metrics_server
from dku_utils.cluster import make_overrides, get_connection_info
from dku_utils.access import _is_none_or_blank
from dku_utils.config_parser import get_security_groups_arg, get_region_arg, get_private_ip_from_metadata
from dku_utils.node_pool import get_node_pool_args

class MyCluster(Cluster):
    def __init__(self, cluster_id, cluster_name, config, plugin_config, global_settings):
        self.cluster_id = cluster_id
        self.cluster_name = cluster_name
        self.config = config
        self.plugin_config = plugin_config
        self.global_settings = global_settings

    def start(self):
        dku_utils.tools_version.check_versions()
        connection_info = get_connection_info(self.config)
        networking_settings = self.config["networkingSettings"]

        has_autoscaling = False
        has_gpu = False
        
        attach_vm_to_security_groups = False
        injected_security_group = self.config.get('injectedSG', '').strip()
       
        if self.config.get('advanced', False):
            has_autoscaling = self.config.get('clusterAutoScaling')
            has_gpu = self.config.get("advancedGPU")

            # create the cluster directly from a yaml def
            yaml_dict = yaml.safe_load(self.config.get("advancedYaml"))

        else:
            node_pool = self.config.get('nodePool', {})

            has_autoscaling = node_pool.get('numNodesAutoscaling', False)
            has_gpu = node_pool.get("enableGPU", False)

            # build the yaml def. As a first step we run eksctl with
            # as many command line args as possible to get it to produce
            # a good base for the cluster yaml def, then we spice it up
            # according to the settings that don't have a command-line 
            # arg
            args = ['create', 'cluster']
            args = args + ['-v', '3'] # not -v 4 otherwise there is a debug line in the beginning of the output
            args = args + ['--name', self.cluster_id]
            args = args + get_region_arg(connection_info)
            args = args + ['--full-ecr-access']

            subnets = networking_settings.get('subnets', [])
            if networking_settings.get('privateNetworking', False):
                args = args + ['--node-private-networking']
                private_subnets = networking_settings.get('privateSubnets', [])
                if len(private_subnets) > 0:
                    args = args + ['--vpc-private-subnets', ','.join(private_subnets)]
            if len(subnets) > 0:
                args = args + ['--vpc-public-subnets', ','.join(subnets)]

            args += get_security_groups_arg(networking_settings)

            args += get_node_pool_args(node_pool)

            k8s_version = self.config.get("k8sVersion", None)
            if not _is_none_or_blank(k8s_version):
                args = args + ['--version', k8s_version.strip()]
                
            c = EksctlCommand(args + ["--dry-run"], connection_info)
            yaml_spec = c.run_and_get_output()
            logging.info("Got spec:\n%s" % yaml_spec)
            
            yaml_dict = yaml.safe_load(yaml_spec)
            
            if self.config.get('privateCluster', False):
                logging.info("Making the cluster fully-private")
                
                private_cluster = yaml_dict.get('privateCluster', {})
                yaml_dict['privateCluster'] = private_cluster
                private_cluster['enabled'] = True
                if self.config.get('skipEndpointCreation', False):
                    private_cluster['skipEndpointCreation'] = True
                else:
                    private_cluster['skipEndpointCreation'] = False
                    if has_autoscaling:
                        private_cluster["additionalEndpointServices"] = private_cluster.get('additionalEndpointServices', [])
                        if not 'autoscaling' in private_cluster["additionalEndpointServices"]:
                            private_cluster["additionalEndpointServices"].append('autoscaling')
                        
                # clear the vpc.clusterEndpoints 
                yaml_dict['vpc'] = yaml_dict.get('vpc', {})
                yaml_dict['vpc']['clusterEndpoints'] = None
                
            # make sure we have a security group to use as shared security group
            # the issue being that eksctl puts this guy on the private VPC endpoints
            # and if you don't control it, then the DSS VM will have no access to the 
            # endpoints, and eksctl will start failing on calls to EC2
            control_plane_security_group = networking_settings.get('controlPlaneSG', '').strip()
            shared_security_group = networking_settings.get('sharedSG', '').strip()
            if len(control_plane_security_group) > 0:
                yaml_dict['vpc']['securityGroup'] = control_plane_security_group
            elif len(shared_security_group) > 0:
                yaml_dict['vpc']['sharedNodeSecurityGroup'] = shared_security_group
            elif self.config.get('privateCluster', False):
                # we'll need to make eksctl able to reach the stuff bearing the 
                # SG created by eksctl
                attach_vm_to_security_groups = True
                
            def add_pre_bootstrap_commands(commands, yaml_dict):
                for node_pool_dict in yaml_dict['managedNodeGroups']:
                    if node_pool_dict.get('preBootstrapCommands') is None:
                        node_pool_dict['preBootstrapCommands'] = []
                    for command in commands.split('\n'):
                        if len(command.strip()) > 0:
                            node_pool_dict['preBootstrapCommands'].append(command)
                
            if node_pool.get('addPreBootstrapCommands', False) and not _is_none_or_blank(node_pool.get("preBootstrapCommands", "")):
                # has to be added in the yaml, there is no command line flag for that
                commands = node_pool.get("preBootstrapCommands", "")
                add_pre_bootstrap_commands(commands, yaml_dict)

        # whatever the setting, make the cluster from the yaml config
        yaml_loc = os.path.join(os.getcwd(), self.cluster_id +'_config.yaml')
        with open(yaml_loc, 'w') as outfile:
            yaml.dump(yaml_dict, outfile, default_flow_style=False)
        logging.info("Final spec\n%s" % yaml.dump(yaml_dict))

        args = ['create', 'cluster']
        args = args + ['-v', '4']
        args = args + ['-f', yaml_loc]

        # we don't add the context to the main config file, to not end up with an oversized config,
        # and because 2 different clusters could be concurrently editing the config file
        kube_config_path = os.path.join(os.getcwd(), 'kube_config')
        args = args + ['--kubeconfig', kube_config_path]

        # if a previous kubeconfig exists, it will be merged with the current configuration, possibly keeping unwanted configuration
        # deleting it ensures a coherent configuration for the cluster
        if os.path.isfile(kube_config_path):
            os.remove(kube_config_path)

        if len(injected_security_group) > 0 or attach_vm_to_security_groups:
            # we'll sniff the stack of the cluster and wait for its shared SG id.
            # It'd have been nice if publicAccessCIDRs could do it automatically
            # but the cloudformation fails on private CIDRs in this field
            def add_vm_to_sg():
                stack_name = None
                # first pester eksctl until it can give the stack name
                # (this is normally when the EKS cluster object is ready)
                stack_name_args = ['utils', 'describe-stacks']
                stack_name_args = stack_name_args + ['--cluster', self.cluster_id]
                stack_name_args = stack_name_args + get_region_arg(connection_info)
                stack_name_args = stack_name_args + ['--output', 'json']
                while stack_name is None:
                    time.sleep(5)
                    try:
                        stack_name_c = EksctlCommand(stack_name_args, connection_info)
                        stack_spec = stack_name_c.run_and_get_output()
                        stack_name = json.loads(stack_spec)[0]["StackName"]
                    except:
                        logging.info("Not yet able to get stack name")
                logging.info("Stack name is %s" % stack_name)
                # then describe the stack resources to get the shared sg. It should be ready
                # (you can't wait for the outputs, they're only available when the cluster is
                # done starting, and that's too late for eksctl)
                sg_ids = []
                for resource_id in ["ControlPlaneSecurityGroup", "ClusterSharedNodeSecurityGroup"]:
                    describe_resource_args = ['cloudformation', 'describe-stack-resource']
                    describe_resource_args = describe_resource_args + get_region_arg(connection_info)
                    describe_resource_args = describe_resource_args + ['--stack-name', stack_name]
                    describe_resource_args = describe_resource_args + ['--logical-resource-id', resource_id]
                    describe_resource_c = AwsCommand(describe_resource_args, connection_info)
                    try:
                        describe_resource = json.loads(describe_resource_c.run_and_get_output()).get('StackResourceDetail', {})
                        sg_id = describe_resource.get("PhysicalResourceId", None)
                        logging.info("%s SG is %s" % (resource_id, sg_id))
                        if sg_id is not None and sg_id != injected_security_group:
                            sg_ids.append(sg_id)
                    except:
                        logging.warn("Not able to get SG id for %s" % resource_id)
                        
                # attach a rule to the shared SG so that the DSS VM can access it (and the VPC endpoints that use it)
                if len(injected_security_group) > 0:
                    inbound = ['--source-group', injected_security_group]
                else:
                    # if no sg has been given for the VM, use a CIDR with an IP
                    private_ip = get_private_ip_from_metadata()
                    inbound = ['--cidr', "%s/32" % private_ip]
                    
                logging.info("Add SG=%s to inbound of SG" % injected_security_group)
                for sg_id in sg_ids:
                    add_sg_rule_args = ['ec2', 'authorize-security-group-ingress']
                    add_sg_rule_args = add_sg_rule_args + get_region_arg(connection_info)
                    add_sg_rule_args = add_sg_rule_args + ['--group-id', sg_id]
                    add_sg_rule_args = add_sg_rule_args + ['--protocol', "all"]
                    add_sg_rule_args = add_sg_rule_args + inbound
                    add_sg_rule_c = AwsCommand(add_sg_rule_args, connection_info)
                    if add_sg_rule_c.run_and_log() != 0:
                        logging.info("Failed to add security group rule")

            t = threading.Thread(target=add_vm_to_sg)
            t.daemon = True
            t.start()
            
        c = EksctlCommand(args, connection_info)
        if c.run_and_log() != 0:
            raise Exception("Failed to start cluster")

        # if you leave eksctl work, you have a public/private EKS endpoint, so we can tighten it even more
        if self.config.get('makePrivateOnly', False):
            privatize_args = ['utils', 'update-cluster-endpoints']
            privatize_args = privatize_args + ['--name', self.cluster_id]
            privatize_args = privatize_args + ['--private-access=true', '--public-access=false']
            privatize_args = privatize_args + ['--approve']
            privatize_args = privatize_args + get_region_arg(connection_info)
            privatize_c = EksctlCommand(privatize_args, connection_info)
            if privatize_c.run_and_log() != 0:
                raise Exception("Failed to make cluster fully private")

        args = ['get', 'cluster']
        args = args + ['--name', self.cluster_id]
        args = args + get_region_arg(connection_info)
        args = args + ['-o', 'json']

        setup_creds_env(kube_config_path, connection_info, self.config)

        if has_autoscaling:
            logging.info("Nodegroup is autoscaling, ensuring autoscaler")
            add_autoscaler_if_needed(self.cluster_id, kube_config_path)
        if has_gpu:
            logging.info("Nodegroup is GPU-enabled, ensuring NVIDIA GPU Drivers")
            add_gpu_driver_if_needed(self.cluster_id, kube_config_path, connection_info)

        if self.config.get('installMetricsServer'):
            install_metrics_server(kube_config_path)

        c = EksctlCommand(args, connection_info)
        cluster_info = json.loads(c.run_and_get_output())[0]

        with open(kube_config_path, "r") as f:
            kube_config = yaml.safe_load(f)

        # collect and prepare the overrides so that DSS can know where and how to use the cluster
        overrides = make_overrides(self.config, kube_config, kube_config_path)
        return [overrides, {'kube_config_path':kube_config_path, 'cluster':cluster_info}]

    def stop(self, data):
        connection_info = get_connection_info(self.config)

        args = ['delete', 'cluster']
        args = args + ['-v', '4']
        args = args + ['--name', self.cluster_id]
        args = args + get_region_arg(connection_info)
        c = EksctlCommand(args, connection_info)

        if c.run_and_log() != 0:
            raise Exception("Failed to stop cluster")
