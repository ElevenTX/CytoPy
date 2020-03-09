from .panel import ChannelMap
from .gating import Gate
from bson.binary import Binary
import numpy as np
import mongoengine
import pickle


class ClusteringDefinition(mongoengine.Document):
    """
    Defines the methodology and parameters of clustering to apply to an FCS File Group, or in the case of
    meta-clustering, a collection of FCS File Groups from the same FCS Experiment

    Parameters
    ----------
    clustering_uid: str, required
        unique identifier
    method: str, required
        type of clustering performed, either PhenoGraph or FlowSOM
    parameters: list, required
        parameters passed to clustering algorithm (list of tuples)
    features: list, required
        list of channels/markers that clustering is performed on
    transform_method: str, optional, (default:"logicle")
        type of transformation to be applied to data prior to clustering
    root_population: str, required, (default:"root")
        population that clustering is performed on
    cluster_prefix: str, optional, (default: "cluster")
        a prefix to add to the name of each resulting cluster
    meta_method: str, required, (default: False)
        refers to whether the clustering is 'meta-clustering'
    meta_clustering_uid_target: str, optional
        clustering_uid for clustering definition that meta-clustering should target
        in each sample
    """
    clustering_uid = mongoengine.StringField(required=True, unique=True)
    method = mongoengine.StringField(required=True, choices=['PhenoGraph', 'FlowSOM', 'ConsensusClustering'])
    parameters = mongoengine.ListField(required=True)
    features = mongoengine.ListField(required=True)
    transform_method = mongoengine.StringField(required=False, default='logicle')
    root_population = mongoengine.StringField(required=True, default='root')
    cluster_prefix = mongoengine.StringField(required=False, default='cluster')
    meta_method = mongoengine.BooleanField(required=True, default=False)
    meta_clustering_uid_target = mongoengine.StringField(required=False)

    meta = {
        'db_alias': 'core',
        'collection': 'cluster_definitions'
    }


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
    cluster_id = mongoengine.StringField(required=True)
    index = mongoengine.FileField(db_alias='core', collection_name='cluster_indexes')
    n_events = mongoengine.IntField(required=True)
    prop_of_root = mongoengine.FloatField(required=True)
    cluster_experiment = mongoengine.ReferenceField(ClusteringDefinition)
    meta_cluster_id = mongoengine.StringField(required=False)

    def save_index(self, data: np.array) -> None:
        """
        Save the index of data that corresponds to cells belonging to this cluster

        Parameters
        ----------
        data: np.array, required
            Numpy array of single cell events data

        Returns
        -------
        None
        """
        if self.index:
            self.index.replace(Binary(pickle.dumps(data, protocol=2)))
        else:
            self.index.new_file()
            self.index.write(Binary(pickle.dumps(data, protocol=2)))
            self.index.close()

    def load_index(self) -> np.array:
        """
        Load the index of data that corresponds to cells belonging to this cluster

        Returns
        -------
        np.array
            Array of single cell events data
        """
        return pickle.loads(bytes(self.index.read()))


class Population(mongoengine.EmbeddedDocument):
    """
    Cached populations; stores the index of events associated to a population for quick loading.

    Attributes
    ----------
    population_name: str, required
        name of population
    index: FileField
        numpy array storing index of events that belong to population
    prop_of_parent: float, required
        proportion of events as a percentage of parent population
    prop_of_total: float, required
        proportion of events as a percentage of all events
    warnings: list, optional
        list of warnings associated to population
    parent: str, required, (default: "root")
        name of parent population
    children: list, optional
        list of child populations (list of strings)
    geom: list, required
        list of key value pairs (tuples; (key, value)) for defining geom of population e.g.
        the definition for an ellipse that 'captures' the population
    clusters: EmbeddedDocListField
        list of associated Cluster documents
    """
    population_name = mongoengine.StringField()
    index = mongoengine.FileField(db_alias='core', collection_name='population_indexes')
    n = mongoengine.IntField()
    parent = mongoengine.StringField(required=True, default='root')
    prop_of_parent = mongoengine.FloatField()
    prop_of_total = mongoengine.FloatField()
    warnings = mongoengine.ListField()
    geom = mongoengine.ListField()
    clustering = mongoengine.EmbeddedDocumentListField(Cluster)
    clusters = mongoengine.ListField() # NEEDS REMOVING

    def save_index(self, data: np.array) -> None:
        """
        Given a new numpy array of index values, serialise and commit data to database

        Parameters
        ----------
        data: np.array
            Array of index values

        Returns
        -------
        None
        """
        if self.index:
            self.index.replace(Binary(pickle.dumps(data, protocol=2)))
        else:
            self.index.new_file()
            self.index.write(Binary(pickle.dumps(data, protocol=2)))
            self.index.close()

    def load_index(self) -> np.array:
        """
        Retrieve the index values for the given population

        Returns
        -------
        np.array
            Array of index values
        """
        return pickle.loads(bytes(self.index.read()))

    def to_python(self) -> dict:
        """
        Generate a python dictionary object for this population

        Returns
        -------
        dict
            Dictionary representation of population document
        """

        geom = {k: v for k, v in self.geom}
        population = dict(name=self.population_name, prop_of_parent=self.prop_of_parent,
                          prop_of_total=self.prop_of_total, warnings=self.warnings, geom=geom,
                          parent=self.parent, index=self.load_index())
        return population

    def list_clustering_experiments(self) -> list:
        """
        Generate a list of clustering experiment UIDs

        Returns
        -------
        list
            Clustering experiment UIDs
        """
        return [c.cluster_experiment.clustering_uid for c in self.clustering]

    def get_many_clusters(self, clustering_uid: str) -> list:
        """
        Given a clustering UID return the associated clusters

        Parameters
        ----------
        clustering_uid: str
             UID for clusters of interest

        Returns
        -------
        list
            List of cluster documents
        """
        if clustering_uid not in self.list_clustering_experiments():
            raise ValueError(f'Error: a clustering experiment with UID {clustering_uid} does not exist')
        return [c for c in self.clustering if c.cluster_experiment.clustering_uid == clustering_uid]

    def delete_clusters(self, clustering_uid: str) -> None:
        """
        Given a clustering UID, remove associated clusters

        Parameters
        ----------
        clustering_uid: str
            UID for clusters to be removed

        Returns
        -------
        None
        """
        if clustering_uid not in self.list_clustering_experiments():
            raise ValueError(f'Error: a clustering experiment with UID {clustering_uid} does not exist')
        self.clustering = [c for c in self.clustering if c.cluster_experiment.clustering_uid != clustering_uid]

    def replace_cluster_experiment(self, current_uid: str, new_cluster_definition: ClusteringDefinition) -> None:
        """
        Given a clustering UID and new clustering definition, replace the clustering definition
        for all associated clusters

        Parameters
        ----------
        current_uid: str
            UID of clusters to be updated
        new_cluster_definition: ClusteringDefinition
            New clustering definition

        Returns
        -------
        None
        """
        for c in self.clustering:
            try:
                if c.cluster_experiment.clustering_uid == current_uid:
                    c.cluster_experiment = new_cluster_definition
            except mongoengine.errors.DoesNotExist:
                c.cluster_experiment = new_cluster_definition

    def update_cluster(self, cluster_id: str, new_cluster: Cluster) -> None:
        """
        Given the ID for a specific cluster, replace the cluster with a new Cluster document

        Parameters
        ----------
        cluster_id: str
            Cluster ID for cluster to replace
        new_cluster: Cluster
            Cluster document to use for updating cluster

        Returns
        -------
        None
        """
        self.clustering = [c for c in self.clustering if c.cluster_id != cluster_id]
        self.clustering.append(new_cluster)

    def list_clusters(self, meta: bool = True) -> set:
        """
        Returns a set of all existing clusters.

        Parameters
        ----------
        meta: bool
            If True, search is isolated to meta-clusters

        Returns
        -------
        set
            Cluster IDs
        """
        if meta:
            return set([c.meta_cluster_id for c in self.clustering])
        return set([c.cluster_id for c in self.clustering])

    def get_cluster(self, cluster_id: str, meta: bool = True) -> (Cluster, np.array):
        """
        Given a cluster ID return the Cluster document and array of index values

        Parameters
        ----------
        cluster_id: str
            ID for cluster to pull from database
        meta: bool
            If True, search will be isolated to clusters associated to a meta cluster ID

        Returns
        -------
        Cluster, np.array
            Cluster Document, Array of index values
        """

        if meta:
            clusters = [c for c in self.clustering if c.meta_cluster_id == cluster_id]
            assert clusters, f'No such cluster(s) with meta clustering ID {cluster_id}'
            idx = [c.load_index() for c in clusters]
            idx = np.unique(np.concatenate(idx, axis=0), axis=0)
            return clusters, idx
        clusters = [c for c in self.clustering if c.cluster_id == cluster_id]
        assert clusters, f'No such cluster with clustering ID {cluster_id}'
        assert len(clusters) == 1, f'Multiple clusters with ID {cluster_id}'
        return clusters[0], clusters[0].load_index()


class Normalisation(mongoengine.EmbeddedDocument):
    """
    Stores a normalised copy of single cell data
    Attributes:
         data [FileField] - tabular normalised single cell data
         root_population [str] - name of the root population data is derived from
         method [str] - name of normalisation method used
    Methods:
        pull - load data and return as a multi-dimensional numpy array
        put - given a numpy array, save the data to the underlying database a new normalised data matrix
    """
    data = mongoengine.FileField(db_alias='core', collection_name='fcs_file_norm')
    root_population = mongoengine.StringField()
    method = mongoengine.StringField()

    def pull(self, sample: int or None = None) -> np.array:
        """
        Load normalised data
        :param sample: int value; produces a sample of given value
        :return:  Numpy array of events data (normalised)
        """
        data = pickle.loads(self.data.read())
        if sample and sample < data.shape[0]:
            idx = np.random.randint(0, data.shape[0], size=sample)
            return data[idx, :]
        return data

    def put(self, data: np.array, root_population: str, method: str) -> None:
        """
        Save events data to database
        :param data: numpy array of events data
        :param root_population: name of the population data is derived from
        :param method: method used for normalisation process
        :return: None
        """
        if self.data:
            self.data.replace(Binary(pickle.dumps(data, protocol=2)))
        else:
            self.data.new_file()
            self.data.write(Binary(pickle.dumps(data, protocol=2)))
            self.data.close()
        self.root_population = root_population
        self.method = method


class File(mongoengine.EmbeddedDocument):
    """
    Embedded document -> FileGroup
    Document representation of a single FCS file.

    Attributes:
        file_id [str] - unique identifier for fcs file
        file_type [str] - one of either 'complete' or 'control'; signifies the type of data stored
        data [FileField] - numpy array of fcs events data
        norm [EmbeddedDoc] - numpy array of normalised fcs events data
        compensated [bool] - boolean value, if True then data have been compensated
        channel_mappings [list] - list of standarised channel/marker mappings (corresponds to column names of underlying data)

    Methods:
        pull - loads data, returning a multi-dimensional numpy array
        put - given a numpy array, data is serialised and stored
    """
    file_id = mongoengine.StringField(required=True)
    file_type = mongoengine.StringField(default='complete')
    data = mongoengine.FileField(db_alias='core', collection_name='fcs_file_data')
    norm = mongoengine.EmbeddedDocumentField(Normalisation)
    compensated = mongoengine.BooleanField(default=False)
    channel_mappings = mongoengine.EmbeddedDocumentListField(ChannelMap)
    batch = mongoengine.StringField(required=False)

    def pull(self, sample: int or None = None) -> np.array:
        """
        Load raw data
        :param sample: int value; produces a sample of given value
        :return:  Numpy array of events data (raw)
        """
        data = pickle.loads(self.data.read())
        if sample and sample < data.shape[0]:
            idx = np.random.randint(0, data.shape[0], size=sample)
            return data[idx, :]
        return data

    def put(self, data: np.array) -> None:
        """
        Save events data to database
        :param data: numpy array of events data
        :return: None
        """
        if self.data:
            self.data.replace(Binary(pickle.dumps(data, protocol=2)))
        else:
            self.data.new_file()
            self.data.write(Binary(pickle.dumps(data, protocol=2)))
            self.data.close()


class FileGroup(mongoengine.Document):
    """
    Document representation of a file group; a selection of related fcs files (e.g. a sample and it's associated
    controls)

    Attributes:
        primary_id [str] - unique ID to associate to group
        files [EmbeddedDoc] - list of File objects
        flags [str] - warnings associated to file group
        notes [str] - additional free text
        populations [EmbeddedDocList] - populations derived from this file group
        gates [EmbeddedDocList] - gate objects that have been applied to this file group
        collection_datetime [DateTime] - date and time of sample collection
        processing_datetime [DateTime] date and time of sample processing
    Methods:
        delete_populations - delete populations
        update_population - update an existing population with a new Population document
        delete_gates - delete gates
        validity - search flags for the 'invalid', returns False if found
        pull_population - retrieve a population document from database
    """
    primary_id = mongoengine.StringField(required=True)
    files = mongoengine.EmbeddedDocumentListField(File)
    flags = mongoengine.StringField(required=False)
    notes = mongoengine.StringField(required=False),
    collection_datetime = mongoengine.DateTimeField(required=False)
    processing_datetime = mongoengine.DateTimeField(required=False)
    populations = mongoengine.EmbeddedDocumentListField(Population)
    gates = mongoengine.EmbeddedDocumentListField(Gate)
    meta = {
        'db_alias': 'core',
        'collection': 'fcs_files'
    }

    def list_populations(self):
        for p in self.populations:
            yield p.population_name

    def list_gates(self):
        for g in self.gates:
            yield g.gate_name

    def delete_populations(self, populations: list or str) -> None:
        """
        Delete one or more populations from FileGroup
        :param populations: either a list of population names or 'all' to delete all associated populations
        :return: None
        """
        if populations == all:
            self.populations = []
        else:
            self.populations = [p for p in self.populations if p.population_name not in populations]
        self.save()

    def update_population(self, population_name: str, new_population: Population):
        """
        Given an existing population name, replace that population with the new population document
        :param population_name: name of population to be replaced
        :param new_population: updated/new Population document
        :return: None
        """
        self.populations = [p for p in self.populations if p.population_name != population_name]
        self.populations.append(new_population)
        self.save()

    def delete_gates(self, gates: list or str):
        """
        Delete one or many gates from FileGroup
        :param gates: either a list of gate names or 'all' to delete all associated gates
        :return: None
        """
        if gates == all:
            self.gates = []
        else:
            self.gates = [g for g in self.gates if g.gate_name not in gates]
        self.save()

    def validity(self) -> bool:
        """
        If 'invalid' found in Flags, will return False
        :return: True if valid (invalid in flags), else False
        """
        if self.flags is None:
            return True
        if 'invalid' in self.flags:
            return False
        return True

    def get_population(self, population_name: str) -> Population:
        """
        Retrieve a population from the database
        :param population_name: name of population to pull
        :return: Population document
        """
        p = [p for p in self.populations if p.population_name == population_name]
        assert p, f'Population {population_name} does not exist'
        return p[0]

