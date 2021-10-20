# -*- coding: utf-8 -*-
import json
import itertools

from hashlib import md5
from math import isnan, ceil
from copy import deepcopy
from datetime import datetime
from flask import current_app
from importlib import import_module
from fastnumbers import isfloat
from flask_mongoengine import DynamicDocument
from mongoengine import CASCADE, signals
from mongoengine.queryset.manager import queryset_manager
from mongoengine.fields import StringField, BooleanField, DictField
from mongoengine.fields import LazyReferenceField, ReferenceField
from mongoengine.fields import DateTimeField, ListField
from boltons.iterutils import remap
from decimal import Decimal
from pint import UnitRegistry
from pint.unit import UnitDefinition
from pint.converters import ScaleConverter
from pint.errors import DimensionalityError
from uncertainties import ufloat_fromstr
from collections import defaultdict
from pymongo.errors import OperationFailure

from mpcontribs.api import enter, valid_dict, delimiter

quantity_keys = {"display", "value", "error", "unit"}
max_dgts = 6
ureg = UnitRegistry(
    autoconvert_offset_to_baseunit=True,
    preprocessors=[
        lambda s: s.replace("%%", " permille "),
        lambda s: s.replace("%", " percent "),
    ],
)
ureg.default_format = ",P~"

ureg.define(UnitDefinition("percent", "%", (), ScaleConverter(0.01)))
ureg.define(UnitDefinition("permille", "%%", (), ScaleConverter(0.001)))
ureg.define(UnitDefinition("ppm", "ppm", (), ScaleConverter(1e-6)))
ureg.define(UnitDefinition("ppb", "ppb", (), ScaleConverter(1e-9)))
ureg.define("atom = 1")
ureg.define("bohr_magneton = e * hbar / (2 * m_e) = µᵇ = µ_B = mu_B")
ureg.define("electron_mass = 9.1093837015e-31 kg = mₑ = m_e")

COMPONENTS = {
    "structures": ["lattice", "sites", "charge"],
    "tables": ["index", "columns", "data"],
    "attachments": ["mime", "content"],
}


def grouper(n, iterable):
    it = iter(iterable)
    while True:
        chunk = tuple(itertools.islice(it, n))
        if not chunk:
            return
        yield chunk


def format_cell(cell):
    cell = cell.strip()
    if not cell or cell.count(" ") > 1:
        return cell

    q = get_quantity(cell)
    if not q or isnan(q.nominal_value):
        return cell

    q = truncate_digits(q)
    return str(q.nominal_value) if isnan(q.std_dev) else str(q)


def new_error_units(measurement, quantity):
    if quantity.units == measurement.value.units:
        return measurement

    error = measurement.error.to(quantity.units)
    return ureg.Measurement(quantity, error)


def get_quantity(s):
    # 5, 5 eV, 5+/-1 eV, 5(1) eV
    # set uncertainty to nan if not provided
    parts = s.split()
    parts += [None] * (2 - len(parts))
    if isfloat(parts[0]):
        parts[0] += "+/-nan"

    try:
        parts[0] = ufloat_fromstr(parts[0])
        return ureg.Measurement(*parts)
    except ValueError:
        return None


def truncate_digits(q):
    if isnan(q.nominal_value):
        return q

    v = Decimal(str(q.nominal_value))
    vt = v.as_tuple()

    if vt.exponent >= 0:
        return q

    dgts = len(vt.digits)
    dgts = max_dgts if dgts > max_dgts else dgts
    s = f"{v:.{dgts}g}"
    if not isnan(q.std_dev):
        s += f"+/-{q.std_dev:.{dgts}g}"

    if q.units:
        s += f" {q.units}"

    return get_quantity(s)


def get_min_max(sender, paths, project_name):
    group = {"_id": None}

    for path in paths:
        field = f"{path}{delimiter}value"
        for k in ["min", "max"]:
            clean_path = path.replace(delimiter, "__")
            key = f"{clean_path}__{k}"
            group[key] = {f"${k}": f"${field}"}

    pipeline = [{"$match": {"project": project_name}}, {"$group": group}]
    result = list(sender.objects.aggregate(pipeline))
    return None if not result else result[0]


def get_resource(component):
    klass = component.capitalize()
    vmodule = import_module(f"mpcontribs.api.{component}.views")
    Resource = getattr(vmodule, f"{klass}Resource")
    return Resource()


def get_md5(resource, obj, fields):
    d = resource.serialize(obj, fields=fields)
    s = json.dumps(d, sort_keys=True).encode("utf-8")
    return md5(s).hexdigest()


class Contributions(DynamicDocument):
    project = LazyReferenceField(
        "Projects", required=True, passthrough=True, reverse_delete_rule=CASCADE
    )
    identifier = StringField(required=True, help_text="material/composition identifier")
    formula = StringField(help_text="formula (set dynamically if not provided)")
    is_public = BooleanField(
        required=True, default=True, help_text="public/private contribution"
    )
    last_modified = DateTimeField(
        required=True, default=datetime.utcnow, help_text="time of last modification"
    )
    needs_build = BooleanField(default=True, help_text="needs notebook build?")
    data = DictField(
        default=dict, validation=valid_dict, help_text="simple free-form data"
    )
    structures = ListField(
        ReferenceField("Structures", null=True), default=list, max_length=10
    )
    tables = ListField(
        ReferenceField("Tables", null=True), default=list, max_length=10
    )
    attachments = ListField(
        ReferenceField("Attachments", null=True), default=list, max_length=10
    )
    notebook = ReferenceField("Notebooks")
    meta = {
        "collection": "contributions",
        "indexes": [
            "project", "identifier", "formula", "is_public", "last_modified",
            "needs_build", "notebook", {"fields": [(r"data.$**", 1)]},
            # can only use wildcardProjection option with wildcard index on all document fields
            {"fields": [(r"$**", 1)], "wildcardProjection" : {"project": 1}},
        ] + list(COMPONENTS.keys()),
    }

    @queryset_manager
    def objects(doc_cls, queryset):
        return queryset.no_dereference().only(
            "project", "identifier", "formula", "is_public", "last_modified", "needs_build"
        )

    @classmethod
    def post_init(cls, sender, document, **kwargs):
        # replace existing components with according ObjectIds
        for component, fields in COMPONENTS.items():
            lst = document._data.get(component)
            if lst and lst[0].id is None:  # id is None for incoming POST
                resource = get_resource(component)
                for i, o in enumerate(lst):
                    digest = get_md5(resource, o, fields)
                    objs = resource.document.objects(md5=digest)
                    exclude = list(resource.document._fields.keys())
                    obj = objs.exclude(*exclude).only("id").first()
                    if obj:
                        lst[i] = obj.to_dbref()

    @classmethod
    def pre_save_post_validation(cls, sender, document, **kwargs):
        # set formula field
        if hasattr(document, "formula") and not document.formula:
            formulae = current_app.config["FORMULAE"]
            document.formula = formulae.get(document.identifier, document.identifier)

        # project is LazyReferenceField & load columns due to custom queryset manager
        project = document.project.fetch().reload("columns")
        columns = {col.path: col for col in project.columns}

        # run data through Pint Quantities and save as dicts
        def make_quantities(path, key, value):
            key = key.strip()
            if key in quantity_keys or not isinstance(value, (str, int, float)):
                return key, value

            # can't be a quantity if contains 2+ spaces
            str_value = str(value).strip()
            if str_value.count(" ") > 1:
                return key, value

            # don't parse if column.unit indicates string type
            field = delimiter.join(["data"] + list(path) + [key])
            if field in columns:
                if columns[field].unit == "NaN":
                    return key, str_value

            # parse as quantity
            q = get_quantity(str_value)
            if not q:
                return key, value

            # silently ignore "nan"
            if isnan(q.nominal_value):
                return False

            # try compact representation
            qq = q.value.to_compact()
            q = new_error_units(q, qq)

            # reduce dimensionality if possible
            if not q.check(0):
                qq = q.value.to_reduced_units()
                q = new_error_units(q, qq)

            # ensure that the same units are used across contributions
            if field in columns:
                column = columns[field]
                if column.unit != str(q.value.units):
                    try:
                        qq = q.value.to(column.unit)
                        q = new_error_units(q, qq)
                    except DimensionalityError:
                        raise ValueError(
                            f"Can't convert [{q.units}] to [{column.unit}] for {field}!"
                        )

            # significant digits
            q = truncate_digits(q)

            # return new value dict
            display = str(q.value) if isnan(q.std_dev) else str(q)
            value = {
                "display": display,
                "value": q.nominal_value,
                "error": q.std_dev,
                "unit": str(q.units),
            }
            return key, value

        document.data = remap(document.data, visit=make_quantities, enter=enter)
        document.last_modified = datetime.utcnow()
        document.needs_build = True

    @classmethod
    def post_save(cls, sender, document, **kwargs):
        # TODO only parts of this need to run on PUT/update
        if kwargs.get("skip"):
            return

        remaining_time = kwargs.get("remaining_time")
        if remaining_time is not None and remaining_time < 5:
            print("NOT ENOUGH TIME LEFT FOR POST_SAVE!")
            return

        from mpcontribs.api.projects.document import Column, MAX_COLUMNS

        # NOTE columns update below only works for single-project bulk updates
        # project is LazyReferenceField; account for custom query manager
        project = document.project.fetch().reload("columns")
        columns_copy = deepcopy(project.columns)
        columns = {col.path: col for col in project.columns}
        min_max_paths = []

        # set columns field for project
        def update_columns(path, key, value):
            path = delimiter.join(["data"] + list(path) + [key])
            is_quantity = isinstance(value, dict) and quantity_keys.issubset(
                value.keys()
            )
            is_text = bool(
                not is_quantity and isinstance(value, str) and key not in quantity_keys
            )
            if is_quantity or is_text or isinstance(value, bool):
                if path not in columns:
                    columns[path] = Column(path=path)

                    if is_quantity:
                        columns[path].unit = value["unit"]

                if is_quantity:
                    min_max_paths.append(path)

                ncolumns = len(columns)
                if ncolumns > MAX_COLUMNS:
                    raise ValueError(f"Reached maximum number of columns ({MAX_COLUMNS})!")

            return True

        # run update_columns over document data
        if not document.data:
            document.reload("data")

        remap(document.data, visit=update_columns, enter=enter)

        # get and set min/max for all paths
        min_max = get_min_max(sender, min_max_paths, project.name)

        for clean_path in min_max_paths:
            for k in ["min", "max"]:
                path = clean_path.replace(delimiter, "__")
                m = min_max.get(f"{path}__{k}")
                if m is not None:
                    setattr(columns[clean_path], k, m)

        # add/remove columns for other components
        for path in COMPONENTS.keys():
            if path not in columns and getattr(document, path):
                columns[path] = Column(path=path)

        cls.update_project(sender, project, columns_copy, columns.values())

    @classmethod
    def update_project(cls, sender, project, old_columns, new_columns):
        from mpcontribs.api.projects.document import Stats

        # only update columns if needed and unchanged in DB
        if old_columns != new_columns and old_columns == project.reload("columns").columns:
            project.update(columns=new_columns)

        # update stats in project
        ncontribs = sender.objects(project=project.name).count()
        stats_kwargs = {"columns": len(new_columns), "contributions": ncontribs}

        for component in COMPONENTS.keys():
            pipeline = [
                {"$match": {
                    "project": project.name,
                    component: {
                        "$exists": True,
                        "$not": {"$size": 0}
                    }
                }},
                {"$count": "count"}
            ]
            result = list(sender.objects.aggregate(pipeline))
            stats_kwargs[component] = result[0]["count"] if result else 0

        stats = Stats(**stats_kwargs)
        project.update(stats=stats)

    @classmethod
    def pre_delete(cls, sender, document, **kwargs):
        args = list(COMPONENTS.keys())
        document.reload(*args)

        for component in COMPONENTS.keys():
            # check if other contributions exist before deletion!
            for idx, obj in enumerate(getattr(document, component)):
                q = {component: obj.id}
                if sender.objects(**q).count() < 2:
                    obj.delete()

    @classmethod
    def post_delete(cls, sender, document, **kwargs):
        if kwargs.get("skip"):
            return

        remaining_time = kwargs.get("remaining_time")
        if remaining_time is not None and remaining_time < 5:
            print("NOT ENOUGH TIME LEFT FOR POST_DELETE!")
            return

        # reset columns field for project
        project = document.project.fetch().reload("columns")
        columns_copy = deepcopy(project.columns)
        columns = {col.path: col for col in project.columns}
        min_max_paths = []

        for path in list(columns.keys()):
            column = columns[path]

            if not isnan(column.min) and not isnan(column.max):
                min_max_paths.append(path)
            else:
                result = list(sender.objects.aggregate([
                    {"$match": {"project": project.name, path: {"$exists": True}}},
                    {"$count": "count"}
                ]))
                if result and result[0]["count"] < 1:
                    columns.pop(path)

        # get and set min/max for all paths
        min_max = get_min_max(sender, min_max_paths, project.name)

        for clean_path in min_max_paths:
            path = clean_path.replace(delimiter, "__")
            column = columns[clean_path]

            for k in ["min", "max"]:
                if min_max.get(f"{path}__{k}") is None:
                    # just deleted last contribution with this column
                    columns.pop(clean_path)
                    break

        cls.update_project(sender, project, columns_copy, columns.values())


signals.post_init.connect(Contributions.post_init, sender=Contributions)
signals.pre_save_post_validation.connect(
    Contributions.pre_save_post_validation, sender=Contributions
)
signals.post_save.connect(Contributions.post_save, sender=Contributions)
signals.pre_delete.connect(Contributions.pre_delete, sender=Contributions)
signals.post_delete.connect(Contributions.post_delete, sender=Contributions)
