from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Tuple, Type, TypeVar
from uuid import UUID, uuid4

from eventsourcing.domain import Snapshot


@dataclass(frozen=True)
class DomainEvent:
    originator_id: UUID
    originator_version: int
    timestamp: datetime

    @staticmethod
    def create_timestamp() -> datetime:
        return datetime.now(tz=timezone.utc)


TAggregate = TypeVar("TAggregate", bound="Aggregate")


@dataclass(frozen=True)
class Aggregate:
    id: UUID
    version: int
    created_on: datetime
    modified_on: datetime

    def trigger_event(
        self,
        event_class: Type[DomainEvent],
        **kwargs: Any,
    ) -> DomainEvent:
        kwargs = kwargs.copy()
        kwargs.update(
            originator_id=self.id,
            originator_version=self.version + 1,
            timestamp=event_class.create_timestamp(),
        )
        return event_class(**kwargs)

    @classmethod
    def projector(
        cls: Type[TAggregate],
        aggregate: Optional[TAggregate],
        events: Iterable[DomainEvent],
    ) -> Optional[TAggregate]:
        for event in events:
            aggregate = cls.mutate(event, aggregate)
        return aggregate

    @classmethod
    def mutate(cls, event: DomainEvent, aggregate: Any) -> Any:
        """Mutates aggregate with event."""


@dataclass(frozen=True)
class Dog(Aggregate):
    name: str
    tricks: Tuple[str, ...]

    @dataclass(frozen=True)
    class Registered(DomainEvent):
        name: str

    @dataclass(frozen=True)
    class TrickAdded(DomainEvent):
        trick: str

    @classmethod
    def register(cls, name: str) -> DomainEvent:
        return cls.Registered(
            originator_id=uuid4(),
            originator_version=1,
            timestamp=DomainEvent.create_timestamp(),
            name=name,
        )

    def add_trick(self, trick: str) -> DomainEvent:
        return self.trigger_event(self.TrickAdded, trick=trick)

    @classmethod
    def mutate(cls, event: DomainEvent, aggregate: Optional["Dog"]) -> Optional["Dog"]:
        """Mutates aggregate with event."""
        if isinstance(event, cls.Registered):
            return Dog(
                id=event.originator_id,
                version=event.originator_version,
                created_on=event.timestamp,
                modified_on=event.timestamp,
                name=event.name,
                tricks=tuple(),
            )
        elif isinstance(event, cls.TrickAdded):
            assert aggregate is not None
            return Dog(
                id=aggregate.id,
                version=event.originator_version,
                created_on=aggregate.created_on,
                modified_on=event.timestamp,
                name=aggregate.name,
                tricks=aggregate.tricks + (event.trick,),
            )
        elif isinstance(event, Snapshot):
            return Dog(
                id=event.state["id"],
                version=event.state["version"],
                created_on=event.state["created_on"],
                modified_on=event.state["modified_on"],
                name=event.state["name"],
                tricks=tuple(event.state["tricks"]),  # comes back from JSON as a list
            )
        else:
            raise NotImplementedError(event)  # pragma: no cover
