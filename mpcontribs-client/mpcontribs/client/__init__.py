# -*- coding: utf-8 -*-
import os
import json
import fido
import warnings
import pandas as pd

from tqdm.notebook import tqdm
from hashlib import md5
from copy import deepcopy
from urllib.parse import urlparse
from pyisemail import is_email
from pyisemail.diagnosis import BaseDiagnosis
from swagger_spec_validator.common import SwaggerValidationError
from bravado_core.formatter import SwaggerFormat
from bravado.client import SwaggerClient
from bravado.fido_client import FidoClient  # async
from bravado.http_future import HttpFuture
from bravado.swagger_model import Loader
from bravado.config import bravado_config_from_config_dict
from bravado_core.spec import Spec
from json2html import Json2Html
from IPython.display import display, HTML
from boltons.iterutils import remap
from pymatgen import Structure


DEFAULT_HOST = "api.mpcontribs.org"
HOST = os.environ.get("MPCONTRIBS_API_HOST", DEFAULT_HOST)
BULMA = "is-narrow is-fullwidth has-background-light"

j2h = Json2Html()
quantity_keys = {"display", "value", "unit"}
pd.options.plotting.backend = "plotly"
warnings.formatwarning = lambda msg, *args, **kwargs: f"{msg}\n"
warnings.filterwarnings("default", category=DeprecationWarning, module=__name__)


def get_md5(d):
    s = json.dumps(d, sort_keys=True).encode("utf-8")
    return md5(s).hexdigest()


def validate_email(email_string):
    d = is_email(email_string, diagnose=True)
    if d > BaseDiagnosis.CATEGORIES["VALID"]:
        raise SwaggerValidationError(f"{email_string} {d.message}")


email_format = SwaggerFormat(
    format="email",
    to_wire=str,
    to_python=str,
    validate=validate_email,
    description="e-mail address",
)


def validate_url(url_string, qualifying=("scheme", "netloc")):
    tokens = urlparse(url_string)
    if not all([getattr(tokens, qual_attr) for qual_attr in qualifying]):
        raise SwaggerValidationError(f"{url_string} invalid")


url_format = SwaggerFormat(
    format="url", to_wire=str, to_python=str, validate=validate_url, description="URL",
)


def chunks(l, n=250):
    n = max(1, n)
    for i in range(0, len(l), n):
        yield l[i : i + n]


class FidoClientGlobalHeaders(FidoClient):
    def __init__(self, headers=None):
        super().__init__()
        self.headers = headers or {}

    def request(self, request_params, operation=None, request_config=None):
        request_for_twisted = self.prepare_request_for_twisted(request_params)
        request_for_twisted["headers"].update(self.headers)
        future_adapter = self.future_adapter_class(fido.fetch(**request_for_twisted))
        return HttpFuture(
            future_adapter, self.response_adapter_class, operation, request_config
        )


def visit(path, key, value):
    if isinstance(value, dict) and quantity_keys == set(value.keys()):
        return key, value["display"]
    return True


class Dict(dict):
    def pretty(self, attrs=f'class="table {BULMA}"'):
        return display(
            HTML(j2h.convert(json=remap(self, visit=visit), table_attributes=attrs))
        )


def load_client(apikey=None, headers=None, host=HOST):
    warnings.warn(
        "load_client(...) is deprecated, use Client(...) instead", DeprecationWarning
    )


class Client(SwaggerClient):
    """client to connect to MPContribs API

    We only want to load the swagger spec from the remote server when needed and not everytime the
    client is initialized. Hence using the Borg design nonpattern (instead of Singleton): Since the
    __dict__ of any instance can be re-bound, Borg rebinds it in its __init__ to a class-attribute
    dictionary. Now, any reference or binding of an instance attribute will actually affect all
    instances equally.
    """

    _shared_state = {}

    def __init__(self, apikey=None, headers=None, host=HOST):
        # - Kong forwards consumer headers when api-key used for auth
        # - forward consumer headers when connecting through localhost
        self.__dict__ = self._shared_state
        self.apikey = apikey
        self.headers = {"x-api-key": apikey} if apikey else headers
        self.host = host

        if "swagger_spec" not in self.__dict__ or (
            self.headers is not None
            and self.swagger_spec.http_client.headers != self.headers
        ):
            self.load()

    def load(self):
        http_client = FidoClientGlobalHeaders(headers=self.headers)
        loader = Loader(http_client)
        protocol = "https" if self.apikey else "http"
        origin_url = f"{protocol}://{self.host}/apispec.json"
        spec_dict = loader.load_spec(origin_url)
        spec_dict["host"] = self.host
        spec_dict["schemes"] = [protocol]

        config = {
            "validate_responses": False,
            "use_models": False,
            "include_missing_properties": False,
            "formats": [email_format, url_format],
        }
        bravado_config = bravado_config_from_config_dict(config)
        for key in set(bravado_config._fields).intersection(set(config)):
            del config[key]
        config["bravado"] = bravado_config

        swagger_spec = Spec.from_dict(spec_dict, origin_url, http_client, config)
        super().__init__(
            swagger_spec, also_return_response=bravado_config.also_return_response
        )

        # expand regex-based query parameters for `data` columns
        resp = self.projects.get_entries(_fields=["columns"]).result()
        columns = {"text": [], "number": []}
        for project in resp["data"]:
            for column in project["columns"]:
                if column["path"].startswith("data."):
                    col = column["path"].replace(".", "__")
                    if column["unit"] == "NaN":
                        columns["text"].append(col)
                    else:
                        col = f"{col}__value"
                        columns["number"].append(col)

        operators = {"text": ["contains"], "number": ["gte", "lte"]}
        for path, d in spec_dict["paths"].items():
            for verb in ["get", "put", "post", "delete"]:
                if verb in d:
                    old_params = deepcopy(d[verb].pop("parameters"))
                    new_params = []

                    while old_params:
                        param = old_params.pop()
                        if param["name"].startswith("^data__"):
                            op = param["name"].rsplit("__", 1)[1]
                            for typ, ops in operators.items():
                                if op in ops:
                                    for column in columns[typ]:
                                        new_param = deepcopy(param)
                                        new_param["name"] = f"{column}__{op}"
                                        desc = f"filter {column} via ${op}"
                                        new_param["description"] = desc
                                        new_params.append(new_param)
                        else:
                            new_params.append(param)

                    d[verb]["parameters"] = new_params

        swagger_spec = Spec.from_dict(spec_dict, origin_url, http_client, config)
        super().__init__(
            swagger_spec, also_return_response=bravado_config.also_return_response
        )

    def get_project(self, project):
        """Convenience function to get full project entry and display as HTML table"""
        return Dict(self.projects.get_entry(pk=project, _fields=["_all"]).result())

    def get_contribution(self, cid):
        """Convenience function to get full contribution entry and display as HTML table"""
        fields = list(
            self.swagger_spec.definitions.get("ContributionsSchema")._properties.keys()
        )
        fields.remove("notebook")
        return Dict(self.contributions.get_entry(pk=cid, _fields=fields).result())

    def get_table(self, tid):
        """Convenience function to get full Pandas DataFrame for a table."""
        table = {"data": []}
        resp = self.tables.get_entry(
            pk=tid, _fields=["total_data_rows", "columns"], data_per_page=1
        ).result()
        table["columns"] = resp["columns"]
        page, pages = 1, None

        with tqdm(total=resp["total_data_rows"]) as pbar:
            while pages is None or page <= pages:
                resp = self.tables.get_entry(
                    pk=tid, _fields=["_all"], data_page=page, data_per_page=1000
                ).result()
                table["data"].extend(resp["data"])
                if pages is None:
                    pages = resp["total_data_pages"]
                page += 1
                pbar.update(len(resp["data"]))

        return pd.DataFrame.from_records(
            table["data"], columns=table["columns"], index=table["columns"][0]
        )

    def get_structure(self, sid):
        """Convenience function to get pymatgen structure."""
        return Structure.from_dict(
            self.structures.get_entry(
                pk=sid, _fields=["lattice", "sites", "charge"]
            ).result()
        )

    def delete_contributions(self, project):
        """Convenience function to remove all contributions for a project"""
        resp = self.contributions.get_entries(
            project=project, _fields=["id"], _limit=1
        ).result()
        ncontribs = resp["total_count"]
        has_more, limit = True, 250

        print(f"Delete {ncontribs} contributions ...")
        with tqdm(total=ncontribs) as pbar:
            while has_more:
                resp = self.contributions.delete_entries(
                    project=project, _limit=limit
                ).result()
                has_more = resp["has_more"]
                pbar.update(resp["count"])

    def submit_contributions(self, contributions, ignore=False):
        """Convenience function to submit a list of contributions"""
        # prepare structures/tables
        existing, md5s = set(), set()
        contribs = deepcopy(contributions)
        ncontribs = len(contribs)
        name = contribs[0]["project"]
        resp = self.projects.get_entry(pk=name, _fields=["unique_identifiers"]).result()

        if resp["unique_identifiers"]:
            print(f"Get existing contributions ...")
            with tqdm(total=ncontribs) as pbar:
                has_more = True
                while has_more:
                    skip = len(existing)
                    resp = self.contributions.get_entries(
                        project=name, _skip=skip, _limit=250, _fields=["identifier"]
                    ).result()
                    existing |= set(c["identifier"] for c in resp["data"])
                    has_more = resp["has_more"]
                    pbar.update(250)

            print(len(existing), "contributions already uploaded.")

        print(f"Prepare {ncontribs} contributions ...")
        with tqdm(total=ncontribs) as pbar:
            for contrib in contribs:
                if contrib["identifier"] in existing:
                    continue

                # TODO prepare tables
                structures = contrib.pop("structures", [])
                contrib["structures"] = []
                nstruc = len(structures)
                for structure in structures:
                    if not isinstance(structure, Structure):
                        raise ValueError("Only accepting pymatgen Structures!")

                    sdct = structure.as_dict()
                    del sdct["@module"]
                    del sdct["@class"]
                    digest = get_md5(sdct)
                    comp = structure.composition.get_integer_formula_and_factor()[0]
                    sdct["name"] = f"{comp}-{nstruc}"
                    msg = f"Duplicate structure {sdct['name']}!"

                    if digest not in md5s:
                        md5s.add(digest)
                        resp = self.structures.get_entries(
                            md5=digest, _fields=["id"], _limit=1
                        ).result()

                        if resp["data"]:
                            print(msg)
                            if not ignore:
                                raise ValueError(msg)
                        else:
                            contrib["structures"].append(sdct)
                    else:
                        print(msg)
                        if not ignore:
                            raise ValueError(msg)

                pbar.update(1)

        print(f"Submit {ncontribs} contributions ...")
        with tqdm(total=ncontribs) as pbar:
            for chunk in chunks(contribs):
                resp = self.contributions.create_entries(contributions=chunk).result()
                pbar.update(resp["count"])
