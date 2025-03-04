import inspect
import logging
import typing as t
from contextlib import contextmanager
from copy import copy
from functools import wraps

from google.protobuf import json_format
from google.protobuf.descriptor import Descriptor
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.descriptor_pool import DescriptorPool
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp
from pyspark.sql import Column
from pyspark.sql import DataFrame
from pyspark.sql.functions import col
from pyspark.sql.functions import struct
from pyspark.sql.functions import udf
from pyspark.sql.types import ArrayType
from pyspark.sql.types import BinaryType
from pyspark.sql.types import BooleanType
from pyspark.sql.types import DataType
from pyspark.sql.types import DoubleType
from pyspark.sql.types import FloatType
from pyspark.sql.types import IntegerType
from pyspark.sql.types import LongType
from pyspark.sql.types import Row
from pyspark.sql.types import StringType
from pyspark.sql.types import StructField
from pyspark.sql.types import StructType
from pyspark.sql.types import TimestampType

from pbspark._timestamp import _from_datetime
from pbspark._timestamp import _to_datetime

# Built in types like these have special methods
# for serialization via MessageToDict. Because the
# MessageToDict function is an intermediate step to
# JSON, some types are serialized to strings.
_MESSAGETYPE_TO_SPARK_TYPE_MAP: t.Dict[str, DataType] = {
    # google/protobuf/timestamp.proto
    "google.protobuf.Timestamp": StringType(),
    # google/protobuf/duration.proto
    "google.protobuf.Duration": StringType(),
    # google/protobuf/wrappers.proto
    "google.protobuf.DoubleValue": DoubleType(),
    "google.protobuf.FloatValue": FloatType(),
    "google.protobuf.Int64Value": LongType(),
    "google.protobuf.UInt64Value": LongType(),
    "google.protobuf.Int32Value": IntegerType(),
    "google.protobuf.UInt32Value": LongType(),
    "google.protobuf.BoolValue": BooleanType(),
    "google.protobuf.StringValue": StringType(),
    "google.protobuf.BytesValue": BinaryType(),
}

# Protobuf types map to these CPP Types. We map
# them to Spark types for generating a spark schema.
# Note that bytes fields are specified by the `type` attribute in addition to
# the `cpp_type` attribute so there is special handling in the `get_spark_schema`
# method.
_CPPTYPE_TO_SPARK_TYPE_MAP: t.Dict[int, DataType] = {
    FieldDescriptor.CPPTYPE_INT32: IntegerType(),
    FieldDescriptor.CPPTYPE_INT64: LongType(),
    FieldDescriptor.CPPTYPE_UINT32: LongType(),
    FieldDescriptor.CPPTYPE_UINT64: LongType(),
    FieldDescriptor.CPPTYPE_DOUBLE: DoubleType(),
    FieldDescriptor.CPPTYPE_FLOAT: FloatType(),
    FieldDescriptor.CPPTYPE_BOOL: BooleanType(),
    FieldDescriptor.CPPTYPE_ENUM: StringType(),
    FieldDescriptor.CPPTYPE_STRING: StringType(),
}


# region serde overrides
class _Printer(json_format._Printer):  # type: ignore
    """Printer override to handle custom messages and byte fields."""

    def __init__(self, custom_serializers=None, **kwargs):
        self._custom_serializers = custom_serializers or {}
        super().__init__(**kwargs)

    def _MessageToJsonObject(self, message):
        full_name = message.DESCRIPTOR.full_name
        if full_name in self._custom_serializers:
            return self._custom_serializers[full_name](message)
        return super()._MessageToJsonObject(message)

    def _FieldToJsonObject(self, field, value):
        # specially handle bytes before protobuf's method does
        if (
            field.cpp_type == FieldDescriptor.CPPTYPE_STRING
            and field.type == FieldDescriptor.TYPE_BYTES
        ):
            return value
        # don't convert int64s to string (protobuf does this for js precision compat)
        elif field.cpp_type in json_format._INT64_TYPES:
            return value
        return super()._FieldToJsonObject(field, value)


class _Parser(json_format._Parser):  # type: ignore
    """Parser override to handle custom messages."""

    def __init__(self, custom_deserializers=None, **kwargs):
        self._custom_deserializers = custom_deserializers or {}
        super().__init__(**kwargs)

    def ConvertMessage(self, value, message, path):
        full_name = message.DESCRIPTOR.full_name
        if full_name in self._custom_deserializers:
            self._custom_deserializers[full_name](value, message, path)
            return
        with _patched_convert_scalar_field_value():
            super().ConvertMessage(value, message, path)


# protobuf converts to/from b64 strings, but we prefer to stay as bytes.
# we handle bytes parser by decorating to handle byte fields first
def _handle_bytes(func):
    @wraps(func)
    def wrapper(value, field, path, require_str=False):
        if (
            field.cpp_type == FieldDescriptor.CPPTYPE_STRING
            and field.type == FieldDescriptor.TYPE_BYTES
        ):
            return bytes(value)  # convert from bytearray to bytes
        return func(value=value, field=field, path=path, require_str=require_str)

    return wrapper


@contextmanager
def _patched_convert_scalar_field_value():
    """Temporarily patch the scalar field conversion function."""
    convert_scalar_field_value_func = json_format._ConvertScalarFieldValue  # type: ignore[attr-defined]
    json_format._ConvertScalarFieldValue = _handle_bytes(  # type: ignore[attr-defined]
        json_format._ConvertScalarFieldValue  # type: ignore[attr-defined]
    )
    try:
        yield
    finally:
        json_format._ConvertScalarFieldValue = convert_scalar_field_value_func


# endregion


class MessageConverter:
    def __init__(self):
        self._custom_serializers: t.Dict[str, t.Callable] = {}
        self._custom_deserializers: t.Dict[str, t.Callable] = {}
        self._message_type_to_spark_type_map = _MESSAGETYPE_TO_SPARK_TYPE_MAP.copy()
        self.register_timestamp_serializer()
        self.register_timestamp_deserializer()

    def register_serializer(
        self,
        message: t.Type[Message],
        serializer: t.Callable,
        return_type: DataType,
    ):
        """Map a message type to a custom serializer and spark output type.

        The serializer should be a function which returns an object which
        can be coerced into the spark return type.
        """
        full_name = message.DESCRIPTOR.full_name
        self._custom_serializers[full_name] = serializer
        self._message_type_to_spark_type_map[full_name] = return_type

    def unregister_serializer(self, message: t.Type[Message]):
        full_name = message.DESCRIPTOR.full_name
        self._custom_serializers.pop(full_name, None)
        self._message_type_to_spark_type_map.pop(full_name, None)
        if full_name in _MESSAGETYPE_TO_SPARK_TYPE_MAP:
            self._message_type_to_spark_type_map[
                full_name
            ] = _MESSAGETYPE_TO_SPARK_TYPE_MAP[full_name]

    def register_deserializer(self, message: t.Type[Message], deserializer: t.Callable):
        full_name = message.DESCRIPTOR.full_name
        self._custom_deserializers[full_name] = deserializer

    def unregister_deserializer(self, message: t.Type[Message]):
        full_name = message.DESCRIPTOR.full_name
        self._custom_deserializers.pop(full_name, None)

    # region timestamp
    def register_timestamp_serializer(self):
        self.register_serializer(Timestamp, _to_datetime, TimestampType())

    def unregister_timestamp_serializer(self):
        self.unregister_serializer(Timestamp)

    def register_timestamp_deserializer(self):
        self.register_deserializer(Timestamp, _from_datetime)

    def unregister_timestamp_deserializer(self):
        self.unregister_deserializer(Timestamp)

    # endregion

    def message_to_dict(
        self,
        message: Message,
        including_default_value_fields=False,
        preserving_proto_field_name=False,
        use_integers_for_enums=False,
        descriptor_pool=None,
        float_precision=None,
    ):
        """Custom MessageToDict using overridden printer."""
        printer = _Printer(
            custom_serializers=self._custom_serializers,
            including_default_value_fields=including_default_value_fields,
            preserving_proto_field_name=preserving_proto_field_name,
            use_integers_for_enums=use_integers_for_enums,
            descriptor_pool=descriptor_pool,
            float_precision=float_precision,
        )
        return printer._MessageToJsonObject(message=message)

    def parse_dict(
        self,
        value: dict,
        message: Message,
        ignore_unknown_fields: bool = False,
        descriptor_pool: t.Optional[DescriptorPool] = None,
        max_recursion_depth: int = 100,
    ):
        """Custom ParseDict using overridden parser."""
        parser = _Parser(
            custom_deserializers=self._custom_deserializers,
            ignore_unknown_fields=ignore_unknown_fields,
            descriptor_pool=descriptor_pool,
            max_recursion_depth=max_recursion_depth,
        )
        return parser.ConvertMessage(value=value, message=message, path=None)

    def get_spark_schema(
        self,
        descriptor: t.Union[t.Type[Message], Descriptor],
        options: t.Optional[dict] = None,
        _seen_descriptors: t.Optional[set] = None
    ) -> DataType:
        """Generate a spark schema from a message type or descriptor
        Given a message type generated from protoc (or its descriptor),
        create a spark schema derived from the protobuf schema when
        serializing with ``MessageToDict``.
        """
        # track which descriptors have been seen in the current proto graph (for loop identification)
        _seen_descriptors_ = copy(_seen_descriptors) or set()

        options = options or {}
        use_camelcase = not options.get("preserving_proto_field_name", False)
        ignore_deprecated = options.get("ignore_deprecated", False)
        ignore_circular_definitions = options.get("ignore_circular_definitions", False)

        schema = []
        if inspect.isclass(descriptor) and issubclass(descriptor, Message):
            descriptor_ = descriptor.DESCRIPTOR
        else:
            descriptor_ = descriptor  # type: ignore[assignment]
        _seen_descriptors_.add(descriptor_.full_name)

        full_name = descriptor_.full_name
        if full_name in self._message_type_to_spark_type_map:
            return self._message_type_to_spark_type_map[full_name]

        for field in descriptor_.fields:
            field_full_name = field.message_type.full_name
            if field.has_options:
                field_options = field.GetOptions()

                # Optionally ignore deprecated fields
                if field_options.deprecated and ignore_deprecated:
                    continue

            # Check for recursive loops in proto definition
            if field.message_type != None:  # noqa ("is None" is not the same as "!= None" here)
                if field_full_name in _seen_descriptors_:
                    if ignore_circular_definitions:
                        logging.warning(f"Circular protobuf definition detected! Ignoring field: {field_full_name}")
                        continue
                    else:
                        raise ValueError(f"Circular protobuf definition detected: {field_full_name}")

            spark_type: DataType
            if field.cpp_type == FieldDescriptor.CPPTYPE_MESSAGE:
                full_name = field.message_type.full_name
                if full_name in self._message_type_to_spark_type_map:
                    spark_type = self._message_type_to_spark_type_map[full_name]
                else:
                    spark_type = self.get_spark_schema(field.message_type, options, _seen_descriptors_)
            # protobuf converts to/from b64 strings, but we prefer to stay as bytes
            elif (
                field.cpp_type == FieldDescriptor.CPPTYPE_STRING
                and field.type == FieldDescriptor.TYPE_BYTES
            ):
                spark_type = BinaryType()
            else:
                spark_type = _CPPTYPE_TO_SPARK_TYPE_MAP[field.cpp_type]
            if field.label == FieldDescriptor.LABEL_REPEATED:
                spark_type = ArrayType(spark_type, True)
            field_name = field.camelcase_name if use_camelcase else field.name
            schema.append((field_name, spark_type, True))
        struct_args = [StructField(*entry) for entry in schema]
        return StructType(struct_args)

    def get_decoder(
        self, message_type: t.Type[Message], options: t.Optional[dict] = None
    ) -> t.Callable:
        """Create a deserialization function for a message type.

        Create a function that accepts a serialized message bytestring
        and returns a dictionary representing the message.

        The ``options`` arg should be a dictionary for the kwargs passsed
        to ``MessageToDict``.
        """
        kwargs = options or {}

        def decoder(s: bytes) -> dict:
            if isinstance(s, bytearray):
                s = bytes(s)
            return self.message_to_dict(message_type.FromString(s), **kwargs)

        return decoder

    def get_decoder_udf(
        self, message_type: t.Type[Message], options: t.Optional[dict] = None
    ) -> t.Callable:
        """Create a deserialization udf for a message type.

        Creates a function for deserializing messages to dict
        with spark schema for expected output.

        The ``options`` arg should be a dictionary for the kwargs passsed
        to ``MessageToDict``.
        """
        return udf(
            self.get_decoder(message_type=message_type, options=options),
            self.get_spark_schema(descriptor=message_type.DESCRIPTOR, options=options),
        )

    def from_protobuf(
        self,
        data: t.Union[Column, str],
        message_type: t.Type[Message],
        options: t.Optional[dict] = None,
    ) -> Column:
        """Deserialize protobuf messages to spark structs.

        Given a column and protobuf message type, deserialize
        protobuf messages also using our custom serializers.

        The ``options`` arg should be a dictionary for the kwargs passed
        our message_to_dict (same args as protobuf's MessageToDict).
        """
        column = col(data) if isinstance(data, str) else data
        protobuf_decoder_udf = self.get_decoder_udf(message_type, options)
        return protobuf_decoder_udf(column)

    def get_encoder(
        self, message_type: t.Type[Message], options: t.Optional[dict] = None
    ) -> t.Callable:
        kwargs = options or {}

        def encoder(s: dict) -> bytes:
            message = message_type()
            # udf may pass a Row object, but we want to pass a dict to the parser
            if isinstance(s, Row):
                s = s.asDict(recursive=True)
            self.parse_dict(s, message, **kwargs)
            return message.SerializeToString()

        return encoder

    def get_encoder_udf(
        self, message_type: t.Type[Message], options: t.Optional[dict] = None
    ) -> t.Callable:
        return udf(
            self.get_encoder(message_type=message_type, options=options),
            BinaryType(),
        )

    def to_protobuf(
        self,
        data: t.Union[Column, str],
        message_type: t.Type[Message],
        options: t.Optional[dict] = None,
    ) -> Column:
        """Serialize spark structs to protobuf messages.

        Given a column and protobuf message type, serialize
        protobuf messages also using our custom serializers.

        The ``options`` arg should be a dictionary for the kwargs passed
        our parse_dict (same args as protobuf's ParseDict).
        """
        column = col(data) if isinstance(data, str) else data
        protobuf_encoder_udf = self.get_encoder_udf(message_type, options)
        return protobuf_encoder_udf(column)

    def df_from_protobuf(
        self,
        df: DataFrame,
        message_type: t.Type[Message],
        options: t.Optional[dict] = None,
        expanded: bool = False,
    ) -> DataFrame:
        """Decode a dataframe of encoded protobuf.

        If expanded, return a dataframe in which each field is its own column. Otherwise
        return a dataframe with a single struct column named `value`.
        """
        df_decoded = df.select(
            self.from_protobuf(df.columns[0], message_type, options).alias("value")
        )
        if expanded:
            df_decoded = df_decoded.select("value.*")
        return df_decoded

    def df_to_protobuf(
        self,
        df: DataFrame,
        message_type: t.Type[Message],
        options: t.Optional[dict] = None,
        expanded: bool = False,
    ) -> DataFrame:
        """Encode data in a dataframe to protobuf as column `value`.

        If `expanded`, the passed dataframe columns will be packed into a struct before
        converting. Otherwise it is assumed that the dataframe passed is a single column
        of data already packed into a struct.

        Returns a dataframe with a single column named `value` containing encoded data.
        """
        if expanded:
            df_struct = df.select(
                struct([df[c] for c in df.columns]).alias("value")  # type: ignore[arg-type]
            )
        else:
            df_struct = df.select(col(df.columns[0]).alias("value"))
        df_encoded = df_struct.select(
            self.to_protobuf(df_struct.value, message_type, options).alias("value")
        )
        return df_encoded


def from_protobuf(
    data: t.Union[Column, str],
    message_type: t.Type[Message],
    options: t.Optional[dict] = None,
    mc: MessageConverter = None,
) -> Column:
    """Deserialize protobuf messages to spark structs"""
    mc = mc or MessageConverter()
    return mc.from_protobuf(data=data, message_type=message_type, options=options)


def to_protobuf(
    data: t.Union[Column, str],
    message_type: t.Type[Message],
    options: t.Optional[dict] = None,
    mc: MessageConverter = None,
) -> Column:
    """Serialize spark structs to protobuf messages."""
    mc = mc or MessageConverter()
    return mc.to_protobuf(data=data, message_type=message_type, options=options)


def df_from_protobuf(
    df: DataFrame,
    message_type: t.Type[Message],
    options: t.Optional[dict] = None,
    expanded: bool = False,
    mc: MessageConverter = None,
) -> DataFrame:
    """Decode a dataframe of encoded protobuf.

    If expanded, return a dataframe in which each field is its own column. Otherwise
    return a dataframe with a single struct column named `value`.
    """
    mc = mc or MessageConverter()
    return mc.df_from_protobuf(
        df=df, message_type=message_type, options=options, expanded=expanded
    )


def df_to_protobuf(
    df: DataFrame,
    message_type: t.Type[Message],
    options: t.Optional[dict] = None,
    expanded: bool = False,
    mc: MessageConverter = None,
) -> DataFrame:
    """Encode data in a dataframe to protobuf as column `value`.

    If `expanded`, the passed dataframe columns will be packed into a struct before
    converting. Otherwise it is assumed that the dataframe passed is a single column
    of data already packed into a struct.

    Returns a dataframe with a single column named `value` containing encoded data.
    """
    mc = mc or MessageConverter()
    return mc.df_to_protobuf(
        df=df, message_type=message_type, options=options, expanded=expanded
    )
