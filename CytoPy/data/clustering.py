from ..utilities import valid_directory
from .experiments import Experiment
from .fcs import FileGroup
import numpy as np
import mongoengine
import h5py
import os


class Cluster(mongoengine.EmbeddedDocument):
    """
    Represents a single cluster generated by a clustering experiment on a single file

    Parameters
    ----------
    cluster_id: str, required
        name associated to cluster
    index: FileField
        index of cell events associated to cluster (very large array)
    n_events: int, required
        number of events in cluster
    prop_of_root: float, required
        proportion of events in cluster relative to root population
    cluster_experiment: RefField
        reference to ClusteringDefinition
    meta_cluster_id: str, optional
        associated meta-cluster
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        with h5py.File(self._instance.h5path, "r") as f:
            if self.cluster_id in f.keys():
                self.index = f[self.cluster_id][:]
            else:
                self.index = kwargs.get("index", None)

    cluster_id = mongoengine.StringField(required=True, unique=True)
    meta_label = mongoengine.StringField(required=False)
    file = mongoengine.ReferenceField(FileGroup, reverse_delete_rule=mongoengine.CASCADE)
    n_events = mongoengine.IntField(required=True)
    prop_of_root = mongoengine.FloatField(required=True)


class ClusteringExperiment(mongoengine.Document):
    name = mongoengine.StringField(required=True, unique=True)
    data_directory = mongoengine.StringField(required=True, validation=valid_directory)
    features = mongoengine.ListField(required=True)
    transform_method = mongoengine.StringField(required=False, default="logicle")
    root_population = mongoengine.StringField(required=True, default="root")
    clusters = mongoengine.EmbeddedDocumentListField(Cluster)
    experiment = mongoengine.ReferenceField(Experiment, reverse_delete_rule=mongoengine.CASCADE)
    prefix = mongoengine.StringField(default="cluster")

    meta = {
        "db_alias": "core",
        "collection": "clustering_experiments"
    }

    def __init__(self, *args, **values):
        super().__init__(*args, **values)
        self.h5path = os.path.join(self.data_directory, f"ClusteringExp_{self.name}.hdf5")

    def add_cluster(self,
                    cluster_id: str,
                    file: FileGroup,
                    cluster_idx: np.array,
                    root_n: int,
                    meta_label: str or None = None):
        assert cluster_id not in [c.cluster_id for c in self.clusters],\
            f"Cluster ID must be unique: {cluster_id} already exists"
        new_cluster = Cluster(cluster_id=cluster_id,
                              meta_label=meta_label,
                              file=file,
                              n_events=cluster_idx.shape[0],
                              prop_of_root=cluster_idx.shape[0]/root_n,
                              index=cluster_idx)
        self.clusters.append(new_cluster)

    def remove_cluster(self,
                       cluster_id: str or None = None,
                       label: str or None = None):
        if label is not None:
            clusters = [c for c in self.clusters if c.label == label]
            assert len(clusters) != 1, f"No cluster with label {label}"
            cluster_id = clusters[0].cluster_id
        else:
            assert cluster_id, "Must provide either cluster ID or label"
            assert cluster_id in [c.cluster_id for c in self.clusters], f"No cluster with id {cluster_id}"
        with h5py.File(self.h5path, "w") as f:
            del f[cluster_id]
        self.clusters = [c for c in self.clusters if c.cluster_id != cluster_id]

    def label_cluster(self,
                      cluster_id: str,
                      label: str):
        assert label not in [c.label for c in self.clusters], f"Label must be unique: {label} already exists"
        cluster = [c for c in self.clusters if c.cluster_id == cluster_id][0]
        cluster.label = label

    def save(self, *args, **kwargs):
        for c in self.clusters:
            with h5py.File(self.h5path, "w") as f:
                f.create_dataset(c.cluster_id, data=c.index)
        super().save(*args, **kwargs)


def _valid_meta_assignments(cluster_ids: list,
                            target: ClusteringExperiment):
    valid_clusters = [c.cluster_id for c in target.clusters]
    if not all([x in valid_clusters for x in cluster_ids]):
        raise mongoengine.errors.ValidationError("One or more clusters assigned by this meta clustering experiment "
                                                 "are not contained within the target clustering experiment")
