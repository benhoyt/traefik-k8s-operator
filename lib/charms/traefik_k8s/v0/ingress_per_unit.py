# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

r"""# Interface Library for ingress_per_unit.

This library wraps relation endpoints using the `ingress_per_unit` interface
and provides a Python API for both requesting and providing per-unit
ingress.

## Getting Started

To get started using the library, you just need to fetch the library using `charmcraft`.
**Note that you also need to add the `serialized_data_interface` dependency to your
charm's `requirements.txt`.**

```shell
charmcraft fetch-lib charms.traefik_k8s.v0.ingress_per_unit
```

```yaml
requires:
    ingress:
        interface: ingress_per_unit
        limit: 1
```

Then, to initialise the library:

```python
# ...
from charms.traefik_k8s.v0.ingress_per_unit import IngressPerUnitRequirer

class SomeCharm(CharmBase):
  def __init__(self, *args):
    # ...
    self.ingress_per_unit = IngressPerUnitRequirer(self, port=80)
    # The following event is triggered when the ingress URL to be used
    # by this unit of `SomeCharm` changes or there is no longer an ingress
    # URL available, that is, `self.ingress_per_unit` would return `None`.
    self.framework.observe(
        self.ingress_per_unit.on.ingress_changed, self._handle_ingress_per_unit
    )
    # ...

    def _handle_ingress_per_unit(self, event):
        logger.info("This unit's ingress URL: %s", self.ingress_per_unit.url)
```
"""
import logging
import typing
import warnings
from typing import Any, Dict, List, Optional, Tuple, TypeVar, Union

import jsonschema
import yaml
from ops.charm import CharmBase, RelationBrokenEvent, RelationEvent
from ops.framework import EventSource, Object, ObjectEvents
from ops.model import (
    ActiveStatus,
    Application,
    BlockedStatus,
    Relation,
    StatusBase,
    Unit,
    WaitingStatus,
)

# The unique Charmhub library identifier, never change it
LIBID = "7ef06111da2945ed84f4f5d4eb5b353a"  # can't register a library until the charm is in the store 9_9

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 8

log = logging.getLogger(__name__)

# ======================= #
#      LIBRARY GLOBS      #
# ======================= #

RELATION_INTERFACE = "ingress_per_unit"
DEFAULT_RELATION_NAME = RELATION_INTERFACE.replace("_", "-")

INGRESS_REQUIRES_UNIT_SCHEMA = {
    "type": "object",
    "properties": {
        "model": {"type": "string"},
        "name": {"type": "string"},
        "host": {"type": "string"},
        "port": {"type": "integer"},
    },
    "required": ["model", "name", "host", "port"],
}
INGRESS_PROVIDES_APP_SCHEMA = {
    "type": "object",
    "properties": {
        "ingress": {
            "type": "object",
            "patternProperties": {
                "": {
                    "type": "object",
                    "properties": {
                        # Optional key for backwards compatibility
                        # with legacy requirers based on SDI
                        "_supported_versions": {"type": "string"},
                        "url": {"type": "string"},
                    },
                    "required": ["url"],
                }
            },
        }
    },
    "required": ["ingress"],
}


# ======================= #
#          TYPES          #
# ======================= #

try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict  # py35 compat

class RequirerData(TypedDict):
    model: str
    name: str
    host: str
    port: int

RequirerUnitData = Dict[Unit, 'RequirerData']
KeyValueMapping = Dict[str, str]
ProviderApplicationData = Dict[str, KeyValueMapping]


# ======================= #
#  SERIALIZATION UTILS    #
# ======================= #


def _deserialize_data(data):
    return yaml.safe_load(data)


def _serialize_data(data):
    return yaml.safe_dump(data, indent=2)


def _validate_data(data, schema):
    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        raise DataValidationError(data, schema) from e


# ======================= #
#       EXCEPTIONS        #
# ======================= #


class IngressPerUnitException(RuntimeError):
    """Base class for errors raised by Ingress Per Unit."""


class DataValidationError(IngressPerUnitException):
    """Raised when data validation fails on IPU relation data."""


class UnknownUnitException(IngressPerUnitException):
    """Raised when a unit passed to API methods does not belong to the relation."""

    def __init__(self, relation: Relation, unit: Unit):
        super().__init__(relation, unit)


class RelationException(IngressPerUnitException):
    """Base class for relation exceptions from this library.

    Attributes:
        relation: The Relation which caused the exception.
        entity: The Application or Unit which caused the exception.
    """

    def __init__(self, relation: Relation, entity: Union[Application, Unit]):
        super().__init__(relation)
        self.args = (
            "There is an error with the relation {}:{} with {}".format(
                relation.name, relation.id, entity.name
            ),
        )
        self.relation = relation
        self.entity = entity


class RelationDataMismatchError(RelationException):
    """Data from different units do not match where they should."""


class RelationPermissionError(IngressPerUnitException):
    """Ingress is requested to do something for which it lacks permissions."""

    def __init__(self, relation: Relation, entity: Union[Application, Unit], message: str):
        self.args = "Unable to write data to relation '{}:{}' with {}: {}".format(
            relation.name, relation.id, entity.name, message
        )
        self.relation = relation


# ======================= #
#         EVENTS          #
# ======================= #


class RelationAvailableEvent(RelationEvent):
    """Event triggered when a relation is ready for requests."""


class RelationFailedEvent(RelationEvent):
    """Event triggered when something went wrong with a relation."""


class RelationReadyEvent(RelationEvent):
    """Event triggered when a remote relation has the expected data."""


class IngressPerUnitEvents(ObjectEvents):
    """Container for events for IngressPerUnit."""

    available = EventSource(RelationAvailableEvent)
    ready = EventSource(RelationReadyEvent)
    failed = EventSource(RelationFailedEvent)
    broken = EventSource(RelationBrokenEvent)


class IngressPerUnitRequestEvent(RelationEvent):
    """Event representing an incoming request.

    This is equivalent to the "ready" event.
    """


class IngressPerUnitProviderEvents(IngressPerUnitEvents):
    """Container for IUP events."""

    request = EventSource(IngressPerUnitRequestEvent)


class _IngressPerUnitBase(Object):
    """Base class for IngressPerUnit interface classes."""

    _IngressPerUnitEventType = TypeVar("_IngressPerUnitEventType", bound=IngressPerUnitEvents)
    on: _IngressPerUnitEventType

    def __init__(self, charm: CharmBase, relation_name: str = DEFAULT_RELATION_NAME):
        """Constructor for _IngressPerUnitBase.

        Args:
            charm: The charm that is instantiating the instance.
            relation_name: The name of the relation name to bind to
                (defaults to "ingress-per-unit").
        """
        super().__init__(charm, relation_name)
        self.charm: CharmBase = charm

        self.relation_name = relation_name
        self.app = self.charm.app
        self.unit = self.charm.unit

        observe = self.framework.observe
        rel_events = charm.on[relation_name]
        observe(rel_events.relation_created, self._handle_relation)
        observe(rel_events.relation_joined, self._handle_relation)
        observe(rel_events.relation_changed, self._handle_relation)
        observe(rel_events.relation_broken, self._handle_relation_broken)
        observe(charm.on.leader_elected, self._handle_upgrade_or_leader)
        observe(charm.on.upgrade_charm, self._handle_upgrade_or_leader)

    @property
    def relations(self):
        """The list of Relation instances associated with this relation_name."""
        return list(self.charm.model.relations[self.relation_name])

    def _handle_relation(self, event):
        relation = event.relation
        if self.is_ready(relation):
            self.on.ready.emit(relation)
        elif self.is_available(relation):
            self.on.available.emit(relation)
        elif self.is_failed(relation):
            self.on.failed.emit(relation)
        else:
            log.debug(
                "Relation {} is neither ready, nor available, nor failed. "
                "Something fishy's going on...".format(relation)
            )

    def get_status(self, relation: Relation) -> StatusBase:
        """Get the suggested status for the given Relation."""
        if self.is_failed(relation):
            return BlockedStatus(
                "Error handling relation {}:{}".format(relation.name, relation.id)
            )
        elif not self.is_available(relation):
            return WaitingStatus("Waiting on relation {}:{}".format(relation.name, relation.id))
        elif not self.is_ready(relation):
            return WaitingStatus("Waiting on relation {}:{}".format(relation.name, relation.id))
        else:
            return ActiveStatus()

    def _handle_relation_broken(self, event):
        self.on.broken.emit(event.relation)

    def _handle_upgrade_or_leader(self, _):
        pass

    def _emit_request_event(self, event):
        self.on.request.emit(event.relation)

    def is_available(self, relation: Relation = None) -> bool:
        """Check whether the given relation is available.

        Or any relation if not specified.
        """
        if relation is None:
            return any(map(self.is_available, self.relations))

        if not relation.app.name:
            # Juju doesn't provide JUJU_REMOTE_APP during relation-broken
            # hooks. See https://github.com/canonical/operator/issues/693.
            # Relation in the process of breaking cannot be available.
            return False

        return True

    def is_ready(self, relation: Relation = None) -> bool:
        """Checks whether the given relation is ready.

        Or any relation if not specified.
        A given relation is ready if the remote side has sent valid data.
        """
        if relation is None:
            return any(map(self.is_ready, self.relations))
        if relation.app is None:
            # No idea why, but this happened once.
            return False
        if not relation.app.name:  # type: ignore
            # Juju doesn't provide JUJU_REMOTE_APP during relation-broken
            # hooks. See https://github.com/canonical/operator/issues/693
            return False
        return True

    def is_failed(self, _: Relation = None) -> bool:
        """Checks whether the given relation is failed.

        Or any relation if not specified.
        """
        raise NotImplementedError("implement in subclass")


class IngressPerUnitProvider(_IngressPerUnitBase):
    """Implementation of the provider of ingress_per_unit."""

    on = IngressPerUnitProviderEvents()

    def __init__(self, charm: CharmBase, relation_name: str = DEFAULT_RELATION_NAME):
        """Constructor for IngressPerUnitProvider.

        Args:
            charm: The charm that is instantiating the instance.
            relation_name: The name of the relation relation_name to bind to
                (defaults to "ingress-per-unit").
        """
        super().__init__(charm, relation_name)
        observe = self.framework.observe
        observe(self.on.ready, self._emit_request_event)
        observe(self.charm.on[relation_name].relation_joined, self._share_version_info)

    def _share_version_info(self, event):
        """Backwards-compatibility shim for version negotiation.

        Allows older versions of IPU (requirer side) to interact with this
        provider without breaking.
        Will be removed in a future version of this library.
        Do not use.
        """
        relation = event.relation
        if self.charm.unit.is_leader():
            log.info("shared supported_versions shim information")
            relation.data[self.charm.app]["_supported_versions"] = "- v1"

    def is_ready(self, relation: Relation = None) -> bool:
        """Checks whether the given relation is ready.

        Or any relation if not specified.
        A given relation is ready if SOME remote side has sent valid data.
        """
        if relation is None:
            return any(map(self.is_ready, self.relations))

        if not super().is_ready(relation):
            return False

        try:
            _, requirer_unit_data = self._fetch_relation_data(relation)
        except Exception:
            log.exception("Cannot fetch ingress data for the '{}' relation".format(relation))
            return False

        return any(requirer_unit_data.values())

    def is_failed(self, relation: Relation = None) -> bool:
        """Checks whether the given relation is failed.

        Or any relation if not specified.
        """
        if relation is None:
            return any(map(self.is_failed, self.relations))

        if not relation.app.name:  # type: ignore
            # Juju doesn't provide JUJU_REMOTE_APP during relation-broken
            # hooks. See https://github.com/canonical/operator/issues/693
            return False

        if not relation.units:
            # Relations without requiring units cannot be in failed state
            return False

        try:
            # grab the data and validate it; might raise
            _, requirer_unit_data = self._fetch_relation_data(relation, validate=True)
        except DataValidationError as e:
            log.warning("Failed to validate relation data for {} relation: {}".format(relation, e))
            return True

        # verify that all remote units (requirer's side) publish the same model.
        # We do not validate the port because, in case of changes to the configuration
        # of the charm or a new version of the charmed workload, e.g. over an upgrade,
        # the remote port may be different among units.
        expected_model = None  # It may be none for units that have not yet written data

        for remote_unit, remote_unit_data in requirer_unit_data.items():
            if "model" in remote_unit_data:
                remote_model = remote_unit_data["model"]
                if not expected_model:
                    expected_model = remote_model
                elif expected_model != remote_model:
                    raise RelationDataMismatchError(relation, remote_unit)

        return False

    def is_unit_ready(self, relation: Relation, unit: Unit) -> bool:
        """Whether the given unit has shared its side of the data."""
        assert unit in relation.units, "attempting to get ready state for unit that does not belong to relation"
        if relation.data.get(unit, {}).get('data'):
            # TODO consider doing schema-based validation here
            return True
        return False

    def get_data(self, relation: Relation, unit: Unit, validate:bool = False) -> 'RequirerData':
        """Fetch the data shared by this unit via the relation (Requirer side)."""
        data = _deserialize_data(relation.data[unit]['data'])
        if validate:
            _validate_data(data, INGRESS_REQUIRES_UNIT_SCHEMA)
        return data

    def publish_url(self, relation: Relation, unit_name: str, url: str):
        """Publish ingress url to a related unit.

        Assumes that this unit is leader.
        """
        raw_data = relation.data[self.app].get('data', None)
        data = _deserialize_data(raw_data) if raw_data else {'ingress': {}}

        # TODO: is this necessary?
        try:
            _validate_data(data, INGRESS_PROVIDES_APP_SCHEMA)
        except DataValidationError as e:
            log.error(f"unable to publish url to {unit_name}: "
                      f"corrupted application databag")
            return
        data['ingress'][unit_name] = {'url': url}
        relation.data[self.app]['data'] = _serialize_data(data)

    def wipe_ingress_data(self, relation):
        """Remove all published ingress data.

        Assumes that this unit is leader.
        """
        relation.data[self.app]['data'] = ""

    def _fetch_relation_data(
        self, relation: Relation, validate=False
    ) -> Tuple[ProviderApplicationData, RequirerUnitData]:
        """Fetch and validate the databags.

        For the provider side: the application databag.
        For the requirer side: the unit databag.
        """
        this_unit = self.unit
        this_app = self.app

        if not relation.app or not relation.app.name:
            # Handle edge case where remote app name can be missing, e.g.,
            # relation_broken events.
            # FIXME https://github.com/canonical/traefik-k8s-operator/issues/34
            return {}, {}

        provider_app_data = {}
        # we start by looking at the provider's app databag
        if this_unit.is_leader():
            # only leaders can read their app's data
            data = relation.data[this_app].get("data")
            deserialized = {}
            if data:
                deserialized = _deserialize_data(data)
                if validate:
                    _validate_data(deserialized, INGRESS_PROVIDES_APP_SCHEMA)
            provider_app_data = deserialized.get("ingress", {})

        # then look at the requirer's (thus remote) unit databags
        remote_units = [unit for unit in relation.units if unit.app is not this_app]

        requirer_unit_data = {}
        for remote_unit in remote_units:
            remote_data = relation.data[remote_unit].get("data")
            remote_deserialized = {}
            if remote_data:
                remote_deserialized = _deserialize_data(remote_data)
                if validate:
                    _validate_data(remote_deserialized, INGRESS_REQUIRES_UNIT_SCHEMA)
            requirer_unit_data[remote_unit] = remote_deserialized

        return provider_app_data, requirer_unit_data

    def publish_ingress_data(self, relation: Relation, data: ProviderApplicationData):
        """Publish ingress data to the relation databag."""
        if not self.unit.is_leader():
            raise RelationPermissionError(relation, self.unit, "This unit is not the leader")

        wrapped_data = {"ingress": data}

        _validate_data(wrapped_data, INGRESS_PROVIDES_APP_SCHEMA)
        # if all is well, write the data
        relation.data[self.app]["data"] = _serialize_data(wrapped_data)

    @property
    def proxied_endpoints(self) -> dict:
        """The ingress settings provided to units by this provider.

        For example, when this IngressPerUnitProvider has provided the
        `http://foo.bar/my-model.my-app-1` and
        `http://foo.bar/my-model.my-app-2` URLs to the two units of the
        my-app application, the returned dictionary will be:

        ```
        {
            "my-app/1": {
                "url": "http://foo.bar/my-model.my-app-1"
            },
            "my-app/2": {
                "url": "http://foo.bar/my-model.my-app-2"
            }
        }
        ```
        """
        results = {}

        for ingress_relation in self.relations:
            provider_app_data, _ = self._fetch_relation_data(ingress_relation)
            results.update(provider_app_data)

        return results


class IngressPerUnitConfigurationChangeEvent(RelationEvent):
    """Event representing a change in the data sent by the ingress."""


class IngressPerUnitRequirerEvents(IngressPerUnitEvents):
    """Container for IUP events."""

    ingress_changed = EventSource(IngressPerUnitConfigurationChangeEvent)


class IngressPerUnitRequirer(_IngressPerUnitBase):
    """Implementation of the requirer of ingress_per_unit."""

    on = IngressPerUnitRequirerEvents()

    def __init__(
        self,
        charm: CharmBase,
        relation_name: str = DEFAULT_RELATION_NAME,
        *,
        host: str = None,
        port: int = None,
    ):
        """Constructor for IngressRequirer.

        The request args can be used to specify the ingress properties when the
        instance is created. If any are set, at least `port` is required, and
        they will be sent to the ingress provider as soon as it is available.
        All request args must be given as keyword args.

        Args:
            charm: the charm that is instantiating the library.
            relation_name: the name of the relation name to bind to
                (defaults to "ingress-per-unit"; relation must be of interface
                type "ingress_per_unit" and have "limit: 1")
            host: Hostname to be used by the ingress provider to address the
            requirer unit; if unspecified, the pod ip of the unit will be used
            instead
        Request Args:
            port: the port of the service
        """
        super().__init__(charm, relation_name)

        # if instantiated with a port, and we are related, then
        # we immediately publish our ingress data  to speed up the process.
        if port:
            self._auto_data = host, port
        else:
            self._auto_data = None

        # Workaround for SDI not marking the EndpointWrapper as not
        # ready upon a relation broken event
        self.is_relation_broken = False

        self.framework.observe(
            self.charm.on[self.relation_name].relation_changed, self._emit_ingress_change_event
        )
        self.framework.observe(
            self.charm.on[self.relation_name].relation_broken, self._emit_ingress_change_event
        )

    def _handle_relation(self, event):
        super()._handle_relation(event)
        self._publish_auto_data(event.relation)

    def _handle_upgrade_or_leader(self, event):
        for relation in self.relations:
            self._publish_auto_data(relation)

    def _publish_auto_data(self, relation: Relation):
        if self._auto_data and self.is_available(relation):
            self._publish_ingress_data(*self._auto_data)

    @property
    def relation(self) -> Optional[Relation]:
        """The established Relation instance, or None if still unrelated."""
        return self.relations[0] if self.relations else None

    def is_ready(self, relation: Relation = None) -> bool:
        """Checks whether the given relation is ready.

        Or any relation if not specified.
        A given relation is ready if the remote side has sent valid data.
        """
        if super().is_ready(relation) is False:
            return False

        return bool(self.url)

    def is_failed(self, relation: Relation = None) -> bool:
        """Checks whether the given relation is failed.

        Or any relation if not specified.
        """
        if not self.relations:  # can't fail if you can't try
            return False

        if relation is None:
            return any(map(self.is_failed, self.relations))

        if not relation.app.name:  # type: ignore
            # Juju doesn't provide JUJU_REMOTE_APP during relation-broken
            # hooks. See https://github.com/canonical/operator/issues/693
            return False

        if not relation.units:
            return False

        try:
            # grab the data and validate it; might raise
            raw = self.relation.data[self.unit].get("data")
        except Exception:
            log.exception("Error accessing relation databag")
            return True

        if raw:
            # validate data
            data = _deserialize_data(raw)
            try:
                _validate_data(data, INGRESS_REQUIRES_UNIT_SCHEMA)
            except DataValidationError:
                log.exception("Error validating relation data")
                return True

        return False

    def _emit_ingress_change_event(self, event):
        if isinstance(event, RelationBrokenEvent):
            self.is_relation_broken = True

        # TODO Avoid spurious events, emit only when URL changes
        self.on.ingress_changed.emit(self.relation)

    def _publish_ingress_data(self, host: Optional[str], port: int):
        """Publish the data that the provider needs to provide ingress."""
        if not host:
            binding = self.charm.model.get_binding(self.relation_name)
            host = str(binding.network.bind_address)

        data = {
            "model": self.model.name,
            "name": self.unit.name,
            "host": host,
            "port": port,
        }
        self.relation.data[self.unit]["data"] = _serialize_data(data)

    def request(self, *, host: str = None, port: int):
        """Request ingress to this unit.

        Args:
            host: Hostname to be used by the ingress provider to address the
             requirer unit; if unspecified, the pod ip of the unit will be used
             instead
            port: the port of the service (required)
        """
        self._publish_ingress_data(host, port)

    @property
    def urls(self) -> dict:
        """The full ingress URLs to reach every unit.

        May return an empty dict if the URLs aren't available yet.
        """
        relation = self.relation
        if not relation or self.is_relation_broken:
            return {}

        raw = None
        if relation.app.name:  # type: ignore
            # FIXME Workaround for https://github.com/canonical/operator/issues/693
            # We must be in a relation_broken hook
            raw = relation.data.get(relation.app, {}).get("data")

        if not raw:
            return {}

        data = _deserialize_data(raw)
        _validate_data(data, INGRESS_PROVIDES_APP_SCHEMA)

        ingress = data.get("ingress", {})
        return {unit_name: unit_data["url"] for unit_name, unit_data in ingress.items()}

    @property
    def url(self) -> Optional[str]:
        """The full ingress URL to reach the current unit.

        May return None if the URL isn't available yet.
        """
        if not self.urls:
            return None
        return self.urls.get(self.charm.unit.name)
