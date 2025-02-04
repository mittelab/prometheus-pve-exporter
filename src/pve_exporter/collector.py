"""
Prometheus collecters for Proxmox VE cluster.
"""
# pylint: disable=too-few-public-methods

import itertools
from proxmoxer import ProxmoxAPI

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily


class LabelingCollectorBase(object):
    """
    A base class for collectors that label their metrics. Provides methods to
    consistently extract a (sub)set of labels and convert them to a
    representation understandable by Prometheus.

    Subclasses can change the KNOWN_LABEL_KEYS to extend or restrict the labels
    that are generated by default. Labels that require some computation of sorts
    can be generated by overriding get_known_labels.
    """

    KNOWN_LABEL_KEYS = ['id', 'name', 'node', 'template', 'type']

    @classmethod
    def downcast_label_value(cls, val):
        """
        Converts an object into a string for Prometheus; None becomes the empty
        string and True/False plain '1' or '0'.
        """
        if val is None:
            return ''
        elif val is True:
            return '1'
        elif val is False:
            return '0'
        else:
            return str(val)

    def get_known_labels(self, resource, label_keys=None):
        """
        Extracts the labels from resource. Returns a dictionary having
        label_keys as keys, and Python values (not converted to strings).
        Where the label is not applicable (e.g. the 'template' entry for a
        storage resource), the value is None. If label_keys is not provided, it
        defaults to KNOWN_LABEL_KEYS.

        Some notable label values (if present in label_keys):
         - `id`: this doesn't exist for cluster resources, so it will be set to
            "cluster/{cluster_name}" for a cluster.
         - `name`: this doesn't exist for a node or a storage resource, so it
            will be set to the `node` and `storage` value, respectively.
        """
        if label_keys is None:
            label_keys = self.__class__.KNOWN_LABEL_KEYS

        # Copy all the keys that already exist
        labels = {key: resource.get(key) for key in label_keys}

        # Custom treatment for some resources. The storage name is stored in
        # 'storage', the node name is stored in 'node'
        if 'name' in labels:
            if labels.get('type') == 'storage':
                labels['name'] = resource.get('storage')
            elif labels.get('type') == 'node':
                labels['name'] = resource.get('node')

        # The cluster id does not exist, we make one
        if 'id' in labels:
            if labels.get('type') == 'cluster':
                labels['id'] = 'cluster/{:s}'.format(resource.get('name'))

        return labels

    def get_label_values(self, labels, label_keys=None):
        """
        Extracts and formats the values for Prometheus from the specified labels
        dictionary, assuming that the keys are ordered as in keys.
        """
        if label_keys is None:
            label_keys = sorted(labels.keys())
        return [self.__class__.downcast_label_value(labels.get(key)) for key in label_keys]


class StatusCollector(LabelingCollectorBase):
    """
    Collects Proxmox VE Node/VM/CT-Status

    # HELP pve_up Node/VM/CT-Status is online/running
    # TYPE pve_up gauge
    pve_up{id="node/proxmox-host", type="node", name="proxmox-host", node="proxmox-host", template=null} 1.0
    pve_up{id="cluster/pvec", type="cluster", name="pvec", node="proxmox-host", template=null} 1.0
    pve_up{id="lxc/101", type="lxc", name="my-container", node="proxmox-host", template=null} 1.0
    pve_up{id="qemu/102", type="qemu", name="my-vm", node="proxmox-host", template=false} 1.0
    """

    SUPPORTED_UP_RES_TYPES = ['qemu', 'lxc', 'node', 'storage', 'vm', 'cluster']

    def __init__(self, pve):
        super(StatusCollector, self).__init__()
        self._pve = pve

    @classmethod
    def is_entry_up(cls, entry):
        if entry.get('type') not in cls.SUPPORTED_UP_RES_TYPES:
            raise ValueError('Got unexpected status entry type {:s}'.format(
                entry.get('type')))

        status = entry.get('status')
        quorate = entry.get('quorate')
        online = entry.get('online')
        return (quorate or online or status in ['available', 'online', 'running'])

    def collect(self): # pylint: disable=missing-docstring
        # Get all the keys except "UP", which is the value
        label_keys = self.__class__.KNOWN_LABEL_KEYS

        status_metrics = GaugeMetricFamily(
            'pve_up',
            'Node/VM/CT-Status is online/running',
            labels=label_keys)

        processed_ids = set()

        for entry in itertools.chain(self._pve.cluster.resources.get(),
            self._pve.cluster.status.get()):

            labels = self.get_known_labels(entry)
            # Skip entries that are in common between cluster resources and
            # cluster status_metrics
            if labels['id'] in processed_ids:
                continue
            else:
                processed_ids.add(labels['id'])

            is_up = self.__class__.is_entry_up(entry)
            label_values = self.get_label_values(labels, label_keys)
            status_metrics.add_metric(label_values, is_up)

        yield status_metrics

class VersionCollector(object):
    """
    Collects Proxmox VE build information. E.g.:

    # HELP pve_version_info Proxmox VE version info
    # TYPE pve_version_info gauge
    pve_version_info{release="15",repoid="7599e35a",version="4.4"} 1.0
    """

    LABEL_WHITELIST = ['release', 'repoid', 'version']

    def __init__(self, pve):
        self._pve = pve

    def collect(self): # pylint: disable=missing-docstring
        version_items = self._pve.version.get().items()
        version = {key: value for key, value in version_items if key in self.LABEL_WHITELIST}

        labels, label_values = zip(*version.items())
        metric = GaugeMetricFamily(
            'pve_version_info',
            'Proxmox VE version info',
            labels=labels
        )
        metric.add_metric(label_values, 1)

        yield metric


class ClusterNodeCollector(object):
    """
    Collects Proxmox VE cluster node information. E.g.:

    # HELP pve_node_info Node info
    # TYPE pve_node_info gauge
    pve_node_info{id="node/proxmox-host", ip="10.20.30.40", level="c",
        local="1", name="proxmox-host", nodeid="0"} 1.0
    """

    def __init__(self, pve):
        self._pve = pve

    def collect(self): # pylint: disable=missing-docstring
        nodes = [entry for entry in self._pve.cluster.status.get() if entry['type'] == 'node']

        if nodes:
            # Remove superflous keys.
            for node in nodes:
                del node['type']
                del node['online']

            # Yield remaining data.
            labels = nodes[0].keys()
            info_metrics = GaugeMetricFamily(
                'pve_node_info',
                'Node info',
                labels=labels)

            for node in nodes:
                label_values = [str(node[key]) for key in labels]
                info_metrics.add_metric(label_values, 1)

            yield info_metrics


class ClusterInfoCollector(object):
    """
    Collects Proxmox VE cluster information. E.g.:

    # HELP pve_cluster_info Cluster info
    # TYPE pve_cluster_info gauge
    pve_cluster_info{id="cluster/pvec",nodes="2",quorate="1",version="2"} 1.0
    """

    def __init__(self, pve):
        self._pve = pve

    def collect(self): # pylint: disable=missing-docstring
        clusters = [entry for entry in self._pve.cluster.status.get() if entry['type'] == 'cluster']

        if clusters:
            # Remove superflous keys.
            for cluster in clusters:
                del cluster['type']

            # Add cluster-prefix to id.
            for cluster in clusters:
                cluster['id'] = 'cluster/{:s}'.format(cluster['name'])
                del cluster['name']

            # Yield remaining data.
            labels = clusters[0].keys()
            info_metrics = GaugeMetricFamily(
                'pve_cluster_info',
                'Cluster info',
                labels=labels)

            for cluster in clusters:
                label_values = [str(cluster[key]) for key in labels]
                info_metrics.add_metric(label_values, 1)

            yield info_metrics


class ClusterResourcesCollector(LabelingCollectorBase):
    """
    Collects Proxmox VE cluster resources information, i.e. memory, storage, cpu
    usage for cluster nodes and guests.
    """

    def __init__(self, pve):
        super(ClusterResourcesCollector, self).__init__()
        self._pve = pve

    def collect(self): # pylint: disable=missing-docstring
        label_keys = self.__class__.KNOWN_LABEL_KEYS
        metrics = {
            'maxdisk': GaugeMetricFamily(
                'pve_disk_size_bytes',
                'Size of storage device',
                labels=label_keys),
            'disk': GaugeMetricFamily(
                'pve_disk_usage_bytes',
                'Disk usage in bytes',
                labels=label_keys),
            'maxmem': GaugeMetricFamily(
                'pve_memory_size_bytes',
                'Size of memory',
                labels=label_keys),
            'mem': GaugeMetricFamily(
                'pve_memory_usage_bytes',
                'Memory usage in bytes',
                labels=label_keys),
            'netout': GaugeMetricFamily(
                'pve_network_transmit_bytes',
                'Number of bytes transmitted over the network',
                labels=label_keys),
            'netin': GaugeMetricFamily(
                'pve_network_receive_bytes',
                'Number of bytes received over the network',
                labels=label_keys),
            'diskwrite': GaugeMetricFamily(
                'pve_disk_write_bytes',
                'Number of bytes written to storage',
                labels=label_keys),
            'diskread': GaugeMetricFamily(
                'pve_disk_read_bytes',
                'Number of bytes read from storage',
                labels=label_keys),
            'cpu': GaugeMetricFamily(
                'pve_cpu_usage_ratio',
                'CPU usage (value between 0.0 and pve_cpu_usage_limit)',
                labels=label_keys),
            'maxcpu': GaugeMetricFamily(
                'pve_cpu_usage_limit',
                'Maximum allowed CPU usage',
                labels=label_keys),
            'uptime': GaugeMetricFamily(
                'pve_uptime_seconds',
                'Number of seconds since the last boot',
                labels=label_keys),
        }

        info_metrics = {
            'guest': GaugeMetricFamily(
                'pve_guest_info',
                'VM/CT info',
                labels=label_keys),
            'storage': GaugeMetricFamily(
                'pve_storage_info',
                'Storage info',
                labels=label_keys),
        }

        info_lookup = {
            'lxc': info_metrics['guest'],
            'qemu': info_metrics['guest'],
            'storage': info_metrics['storage'],
        }

        for resource in self._pve.cluster.resources.get():
            restype = resource.get('type')

            label_values = self.get_label_values(self.get_known_labels(resource))

            if restype in info_lookup:
                info_lookup[restype].add_metric(label_values, 1)

            for key, metric_value in resource.items():
                if key in metrics:
                    metrics[key].add_metric(label_values, metric_value)

        return itertools.chain(metrics.values(), info_metrics.values())

def collect_pve(config, host):
    """Scrape a host and return prometheus text format for it"""

    pve = ProxmoxAPI(host, **config)

    registry = CollectorRegistry()
    registry.register(StatusCollector(pve))
    registry.register(ClusterResourcesCollector(pve))
    registry.register(ClusterNodeCollector(pve))
    registry.register(ClusterInfoCollector(pve))
    registry.register(VersionCollector(pve))
    return generate_latest(registry)
