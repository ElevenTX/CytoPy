from shapely import affinity
from shapely.geometry import Polygon, Point
from shapely.ops import unary_union
from _warnings import warn
import numpy as np
import mongoengine
import h5py
import os


class PopulationGeometry(mongoengine.EmbeddedDocument):
    """
    Geometric shape generated by non-threshold generating Gate
    """
    x = mongoengine.StringField()
    y = mongoengine.StringField()
    transform_x = mongoengine.StringField()
    transform_y = mongoengine.StringField()
    x_values = mongoengine.ListField()
    y_values = mongoengine.ListField()
    width = mongoengine.FloatField()
    height = mongoengine.FloatField()
    center = mongoengine.ListField()
    angle = mongoengine.FloatField()
    x_threshold = mongoengine.FloatField()
    y_threshold = mongoengine.FloatField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._shape = None

    @property
    def shape(self):
        """
        Generates a Shapely Polygon object.

        Returns
        -------
        Shapely.geometry.Polygon
        """
        if self.x_values and self.y_values:
            return Polygon([(x, y) for x, y in zip(self.x_values, self.y_values)])
        elif all([self.width,
                  self.height,
                  self.center,
                  self.angle]):
            circle = Point(self.center).buffer(1)
            ellipse = affinity.rotate(affinity.scale(circle, self.width, self.height),
                                      self.angle)
            return ellipse
        return None

    def overlap(self,
                comparison_poly: Polygon,
                threshold: float = 0.):
        if self.shape is None:
            warn("PopulationGeometry properties are incomplete. Cannot determine shape.")
            return None
        if self.shape.intersects(comparison_poly):
            overlap = float(self.shape.intersection(comparison_poly).area / self.shape.area)
            if overlap >= threshold:
                return overlap
        return 0.


class Population(mongoengine.EmbeddedDocument):
    """
    Cached populations; stores the index of events associated to a population for quick loading.

    Parameters
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

    def __init__(self, *args, **kwargs):
        # If the Population existed previously, fetched the index
        self._index = None
        self._ctrl_index = dict()
        self.h5path = os.path.join(self._instance.data_directory, f"{self._instance.id.__str__()}.hdf5")
        with h5py.File(self.h5path, "r") as f:
            # Load the population index (if population exists)
            if f'/index/{self.population_name}' in f.keys():
                self._index = f[f'/index/{self.population_name}'][:]
            # Load the control index (if population exists)
            for ctrl in self._instance.controls:
                if f'/index/{self.population_name}/{ctrl}' in f.keys():
                    self._ctrl_index[ctrl] = f[f'/index/{self.population_name}/{ctrl}'][:]
        # If this is a new instance of Population and index has been given in kwargs, set self._index
        if self._index is None and "index" in kwargs.keys():
            self._index = kwargs.pop("index")
        if not self._ctrl_index and "ctrl_index" in kwargs.keys():
            self._ctrl_index = kwargs.pop("ctrl_index")
        super().__init__(*args, **kwargs)

    population_name = mongoengine.StringField()
    n = mongoengine.IntField()
    parent = mongoengine.StringField(required=True, default='root')
    prop_of_parent = mongoengine.FloatField()
    prop_of_total = mongoengine.FloatField()
    warnings = mongoengine.ListField()
    geom = mongoengine.EmbeddedDocumentField(PopulationGeometry)
    definition = mongoengine.StringField()

    @property
    def index(self):
        return self._index

    @index.setter
    def index(self, idx: np.array):
        self.n = len(idx)
        self._index = idx

    @property
    def ctrl_index(self):
        return self._ctrl_index

    @ctrl_index.setter
    def ctrl_index(self, ctrl_idx: tuple):
        assert len(ctrl_idx) == 2, "ctrl_idx should be a tuple of length 2"
        assert type(ctrl_idx[0]) == str, "first item in ctrl_idx should be type str"
        assert type(ctrl_idx[1]) == np.array, "second item in ctrl_idx should be type numpy.array"
        self._ctrl_index[ctrl_idx[0]] = ctrl_idx[1]


def merge_populations(left: Population,
                      right: Population,
                      new_population_name: str or None = None):
    assert left.parent == right.parent, "Parent populations do not match"
    # check that geometries overlap
    has_shape = [p.geom.shape is not None for p in [left, right]]
    new_definition = None
    if new_population_name is None:
        new_population_name = f"merge_{left.population_name}_{right.population_name}"
    assert sum(has_shape) != 1, "To merge populations, both gates must be elliptical or polygon gates or both " \
                                "must be threshold gates. Cannot merge one type with the other."
    assert left.geom.transform_x == left.geom.transform_x, "X dimension transform differs between left and right" \
                                                           "populations"
    assert left.geom.transform_y == left.geom.transform_y, "Y dimension transform differs between left and right" \
                                                           "populations"
    if all(has_shape):
        assert left.geom.shape.intersects(right.geom.shape), "Invalid: cannot merge non-overlapping populations"
    else:
        new_definition = ",".join([left.definition, right.definition])
    # TODO lookup all clusters applied to this population and delete
    warn("Associated clusters are now void. Repeat clustering on new population")
    if len(left.ctrl_index) > 0 or len(right.ctrl_index) > 0:
        warn("Associated control indexes are now void. Repeat control gating on new population")
    new_shape = unary_union([p.geom.shape for p in [left, right]])
    x, y = new_shape.exterior.coords.xy
    new_geom = PopulationGeometry(x=left.geom.x,
                                  y=left.geom.y,
                                  transform_x=left.geom.transform_x,
                                  transform_y=left.geom.transform_y,
                                  x_values=x,
                                  y_values=y)
    new_population = Population(population_name=new_population_name,
                                n=len(left.index) + len(right.index),
                                parent=left.parent,
                                warnings=left.warnings + right.warnings + ["MERGED POPULATION"],
                                index=np.unique(np.concatenate(left.index, right.index)),
                                geom=new_geom,
                                definition=new_definition)
    return new_population