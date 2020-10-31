#!/usr/bin.env/python
# -*- coding: utf-8 -*-
"""
When analysing single cell data we are ultimately interested in populations
of cells. This module contains the Population class, which controls the
data attaining to a single cell population. A FileGroup (see CytoPy.data.fcs)
can contain many Populations (which are embedded within the FileGroup).
These Populations can also contain many Clusters, generated from a
high-dimensional clustering algorithm applied to a population of cells
e.g. FlowSOM or Phenograph. The cluster results are embedded within
the Population as a collection of Clusters.

Copyright 2020 Ross Burton

Permission is hereby granted, free of charge, to any person
obtaining a copy of this software and associated documentation
files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the
Software, and to permit persons to whom the Software is furnished
to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

from ..flow.transforms import scaler
from .geometry import PopulationGeometry, ThresholdGeom, PolygonGeom
from functools import reduce
from shapely.ops import unary_union
from typing import List
from _warnings import warn
import numpy as np
import pandas as pd
import mongoengine

__author__ = "Ross Burton"
__copyright__ = "Copyright 2020, CytoPy"
__credits__ = ["Ross Burton", "Simone Cuff", "Andreas Artemiou", "Matthias Eberl"]
__license__ = "MIT"
__version__ = "1.0.0"
__maintainer__ = "Ross Burton"
__email__ = "burtonrj@cardiff.ac.uk"
__status__ = "Production"


class Cluster(mongoengine.EmbeddedDocument):
    """
    Represents a single cluster generated by a clustering experiment on a single file.
    Clusters are generated by a Clustering object (see CytoPy.flow.clustering.main.Clustering
    for more info).

    Attributes
    ----------
    cluster_id: str, required
        name associated to cluster (must be unique to population)
    meta_label: str
        name of associated meta-cluster to which this cluster belongs
    index: Numpy.Array
        index of cell events associated to cluster (very large array)
    n: int, required
        number of events in cluster
    prop_of_events: float, required
        proportion of events in cluster relative to root population
    tag: str
        identifier for grouping clusters derived from the same analysis/algorithm
    """
    cluster_id = mongoengine.StringField(required=True)
    meta_label = mongoengine.StringField(required=False)
    n = mongoengine.IntField(required=True)
    prop_of_events = mongoengine.FloatField(required=True)
    tag = mongoengine.StringField(required=True)

    def __init__(self, *args, **kwargs):
        self._index = kwargs.pop("index", None)
        super().__init__(*args, **kwargs)

    @property
    def index(self):
        return self._index

    @index.setter
    def index(self, idx: np.array or list):
        self.n = len(idx)
        self._index = np.array(idx)


class Population(mongoengine.EmbeddedDocument):
    """
    A population of cells identified by either a gate or supervised algorithm. Stores the
    index of events corresponding to a single population, where the index relates back
    to the primary data in the FileGroup in which a population is embedded.

    Populations also store Clusters generated from high dimensional clustering algorithms
    such as FlowSOM or PhenoGraph. These clusters are derived from this population.

    Parameters
    ----------
    population_name: str, required
        name of population
    n: int
        number of events associated to this population
    parent: str, required, (default: "root")
        name of parent population
    prop_of_parent: float, required
        proportion of events as a percentage of parent population
    prop_of_total: float, required
        proportion of events as a percentage of all events
    warnings: list, optional
        list of warnings associated to population
    geom: PopulationGeometry
        PopulationGeometry (see CytoPy.data.geometry) that defines the gate that
        captures this population.
    clusters: EmbeddedDocListField
        list of associated Cluster documents
    definition: str
        relevant for populations generated by a ThresholdGate; defines the source of this
        population e.g. "+" for a 1D threshold or "+-" for a 2D threshold
    index: Numpy.Array
        numpy array storing index of events that belong to population
    signature: dict
        average of a population feature space (median of each channel); used to match
        children to newly identified populations for annotating
    """
    population_name = mongoengine.StringField()
    n = mongoengine.IntField()
    parent = mongoengine.StringField(required=True, default='root')
    prop_of_parent = mongoengine.FloatField()
    prop_of_total = mongoengine.FloatField()
    warnings = mongoengine.ListField()
    geom = mongoengine.EmbeddedDocumentField(PopulationGeometry)
    clusters = mongoengine.EmbeddedDocumentListField(Cluster)
    definition = mongoengine.StringField()
    signature = mongoengine.DictField()

    def __init__(self, *args, **kwargs):
        # If the Population existed previously, fetched the index
        self._index = kwargs.pop("index", None)
        self._ctrl_index = kwargs.pop("ctrl_index", dict())
        super().__init__(*args, **kwargs)

    @property
    def index(self):
        return self._index

    @index.setter
    def index(self, idx: np.array):
        assert isinstance(idx, np.ndarray), "idx should be type numpy.array"
        self.n = len(idx)
        self._index = np.array(idx)

    @property
    def ctrl_index(self):
        return self._ctrl_index

    def set_ctrl_index(self, **kwargs):
        for k, v in kwargs.items():
            assert isinstance(v, np.ndarray), "ctrl_idx should be type numpy.array"
            self._ctrl_index[k] = v

    def add_cluster(self,
                    cluster: Cluster):
        """
        Add a new cluster generated from CytoPy.flow.clustering.main.Clustering.

        Parameters
        ----------
        cluster: Cluster

        Returns
        -------
        None
        """
        _id, tag = cluster.cluster_id, cluster.tag
        err = f"Cluster already exists with id: {_id}; tag: {tag}"
        assert not any([x.cluster_id == _id and x.tag == tag for x in self.clusters]), err
        self.clusters.append(cluster)

    def delete_cluster(self,
                       cluster_id: str or None = None,
                       tag: str or None = None,
                       meta_label: str or None = None):
        """
        Delete cluster using either cluster ID, tag, or meta label

        Parameters
        ----------
        cluster_id: str
        tag: str
        meta_label: str

        Returns
        -------
        None
        """
        err = "Must provide either cluster_id, tag or meta_label"
        assert sum([x is not None for x in [cluster_id, tag, meta_label]]) == 1, err
        if cluster_id:
            self.clusters = [c for c in self.clusters if c.cluster_id != cluster_id]
        elif tag:
            self.clusters = [c for c in self.clusters if c.tag != tag]
        elif meta_label:
            self.clusters = [c for c in self.clusters if c.meta_label != meta_label]

    def delete_all_clusters(self,
                            clusters: list or str = "all"):
        """
        Provide either a list of cluster IDs for deletion or give value of "all"
        to delete all clusters.

        Parameters
        ----------
        clusters: list or str (default="all")

        Returns
        -------
        None
        """
        if isinstance(clusters, list):
            self.clusters = [c for c in self.clusters if c.cluster_id not in clusters]
        else:
            self.clusters = []

    def list_clusters(self,
                      tag: str or None = None,
                      meta_label: str or None = None) -> List[str]:
        """
        List cluster IDs associated to a given tag or meta label

        Parameters
        ----------
        tag: str
        meta_label: str

        Returns
        -------
        List
        """
        if tag:
            return [c.cluster_id for c in self.clusters if c.tag == tag]
        elif meta_label:
            return [c.cluster_id for c in self.clusters if c.meta_label == meta_label]
        else:
            return [c.cluster_id for c in self.clusters]

    def get_clusters(self,
                     cluster_id: list or None = None,
                     tag: str or None = None,
                     meta_label: str or None = None) -> List[Cluster]:
        """
        Returns list of cluster objects by either cluster IDs, tag or meta label

        Parameters
        ----------
        cluster_id: list
        tag: str
        meta_label: str

        Returns
        -------
        list
        """
        err = "Provide list of cluster IDs and/or tag and/or meta_label"
        assert len(sum([x is not None for x in [tag, meta_label]])) > 0, err
        clusters = self.clusters
        if cluster_id:
            clusters = [c for c in clusters if c.cluster_id in cluster_id]
        if tag:
            clusters = [c for c in clusters if c.tag in tag]
        if meta_label:
            clusters = [c for c in clusters if c.meta_label in meta_label]
        return clusters


def _check_overlap(left: Population,
                   right: Population,
                   error: bool = True):
    """
    Given two Population objects assuming that they have Polygon geoms (raises assertion error otherwise), checks if the population geometries overlap.
    If error is True, raises assertion error if the geometries do not overlap.

    Parameters
    ----------
    left: Population
    right: Population
    error: bool (default = True)

    Returns
    -------
    bool or None
    """
    assert all(
        [isinstance(x.geom, PolygonGeom) for x in [left, right]]), "Only Polygon geometries can be checked for overlap"
    overlap = left.geom.shape.intersects(right.geom.shape)
    if error:
        assert overlap, "Invalid: non-overlapping populations"
    return overlap


def _check_transforms_dimensions(left: Population,
                                 right: Population):
    """
    Given two Populations, checks if transformation methods and axis match. Raises assertion error if not.

    Parameters
    ----------
    left: Population
    right: Population

    Returns
    -------
    None
    """
    assert left.geom.transform_x == right.geom.transform_x, \
        "X dimension transform differs between left and right populations"
    assert left.geom.transform_y == right.geom.transform_y, \
        "Y dimension transform differs between left and right populations"
    assert left.geom.x == right.geom.x, "X dimension differs between left and right populations"
    assert left.geom.y == right.geom.y, "Y dimension differs between left and right populations"


def _merge_index(left: Population,
                 right: Population) -> np.ndarray:
    """
    Merge the index of two populations.

    Parameters
    ----------
    left: Population
    right: Population

    Returns
    -------
    Numpy.Array
    """
    return np.unique(np.concatenate([left.index, right.index], axis=0), axis=0)


def _merge_signatures(left: Population,
                      right: Population) -> dict:
    """
    Merge the signatures of two populations; taken as the mean of both signatures.

    Parameters
    ----------
    left: Population
    right: Population

    Returns
    -------
    dict
    """
    return pd.DataFrame([left.signature, right.signature]).mean().to_dict()


def _merge_thresholds(left: Population,
                      right: Population,
                      new_population_name: str):
    """
    Merge two Populations with ThresholdGeom geometries.

    Parameters
    ----------
    left: Population
    right: Population
    new_population_name: str

    Returns
    -------
    Population
    """
    assert left.geom.x_threshold == right.geom.x_threshold, \
        "Threshold merge assumes that the populations are derived " \
        "from the same gate; X threshold should match between populations"
    assert left.geom.y_threshold == right.geom.y_threshold, \
        "Threshold merge assumes that the populations are derived " \
        "from the same gate; Y threshold should match between populations"
    if left.clusters or right.clusters:
        warn("Associated clusters are now void. Repeat clustering on new population")
        left.clusters, right_clusters = [], []
    if len(left.ctrl_index) > 0 or len(right.ctrl_index) > 0:
        warn("Associated control indexes are now void. Repeat control gating on new population")
    new_geom = ThresholdGeom(x=left.geom.x,
                             y=left.geom.y,
                             transform_x=left.geom.transform_x,
                             transform_y=left.geom.transform_y,
                             x_threshold=left.geom.x_threshold,
                             y_threshold=left.geom.y_threshold)

    new_population = Population(population_name=new_population_name,
                                n=len(left.index) + len(right.index),
                                parent=left.parent,
                                warnings=left.warnings + right.warnings + ["MERGED POPULATION"],
                                index=_merge_index(left, right),
                                geom=new_geom,
                                definition=",".join([left.definition, right.definition]),
                                signature=_merge_signatures(left, right))
    return new_population


def _merge_polygons(left: Population,
                    right: Population,
                    new_population_name: str):
    """
    Merge two Populations with PolygonGeom geometries.

    Parameters
    ----------
    left: Population
    right: Population
    new_population_name: str

    Returns
    -------
    Population
    """
    _check_overlap(left, right)
    new_shape = unary_union([p.geom.shape for p in [left, right]])
    x, y = new_shape.exterior.coords.xy
    new_geom = PolygonGeom(x=left.geom.x,
                           y=left.geom.y,
                           transform_x=left.geom.transform_x,
                           transform_y=left.geom.transform_y,
                           x_values=x,
                           y_values=y)
    new_idx = _merge_index(left, right)
    new_population = Population(population_name=new_population_name,
                                n=len(new_idx),
                                parent=left.parent,
                                warnings=left.warnings + right.warnings + ["MERGED POPULATION"],
                                index=new_idx,
                                geom=new_geom,
                                signature=_merge_signatures(left, right))
    return new_population


def merge_populations(left: Population,
                      right: Population,
                      new_population_name: str or None = None):
    """
    Merge two Population's. The indexes and signatures of these populations will be merged.
    The populations must have the same geometries.

    Parameters
    ----------
    left: Population
    right: Population
    new_population_name: str

    Returns
    -------
    Population
    """
    _check_transforms_dimensions(left, right)
    new_population_name = new_population_name or f"merge_{left.population_name}_{right.population_name}"
    assert left.parent == right.parent, "Parent populations do not match"
    assert isinstance(left.geom, type(
        right.geom)), f"Geometries must be of the same type; left={type(left.geom)}, right={type(right.geom)}"
    if isinstance(left.geom, ThresholdGeom):
        return _merge_thresholds(left, right, new_population_name)
    return _merge_polygons(left, right, new_population_name)


def merge_multiple_populations(populations: List[Population],
                               new_population_name: str or None = None):
    """
    Merge multiple Population's. The indexes and signatures of these populations will be merged.
    The populations must have the same geometries.

    Parameters
    ----------
    populations: list
    new_population_name: str

    Returns
    -------
    Population
    """
    if new_population_name is None:
        assert len(set([p.population_name for p in populations])) == 1, \
            "If a new population name is not given the populations are expected to have the same population name"
    new_population_name = new_population_name or populations[0].population_name
    merged_pop = reduce(lambda p1, p2: merge_populations(p1, p2), populations)
    merged_pop.population_name = new_population_name
    return merged_pop


def create_signature(data: pd.DataFrame,
                     idx: np.array or None = None,
                     summary_method: callable or None = None) -> dict:
    """
    Given a dataframe of FCS events, generate a signature of those events; that is, a summary of the
    dataframes columns using the given summary method.

    Parameters
    ----------
    data: Pandas.DataFrame
    idx: Numpy.array (optional)
        Array of indexes to be included in this operation, if None, the whole dataframe is used
    summary_method: callable (optional)
        Function to use to summarise columns, defaults is Numpy.median
    Returns
    -------
    dict
        Dictionary representation of signature; {column name: summary statistic}
    """
    if data.shape[0] == 0:
        warn("Cannot generate signature for empty dataframe")
        return {}
    data = pd.DataFrame(scaler(data=data.values, scale_method="norm", return_scaler=False),
                        columns=data.columns,
                        index=data.index)
    if idx is None:
        idx = data.index.values
    # ToDo this should be more robust
    for x in ["Time", "time"]:
        if x in data.columns:
            data.drop(x, 1, inplace=True)
    summary_method = summary_method or np.median
    signature = data.loc[idx].apply(summary_method)
    return {x[0]: x[1] for x in zip(signature.index, signature.values)}