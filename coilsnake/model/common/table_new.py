import logging
import re
import yaml

from coilsnake.exceptions.common.exceptions import InvalidArgumentError, IndexOutOfRangeError
from coilsnake.util.common.helper import getitem_with_default
from coilsnake.util.common.type import GenericEnum, in_range


log = logging.getLogger(__name__)


class BooleanTableEntry(object):
    @classmethod
    def from_block(cls, block, offset):
        return block.read_multi(offset, cls.size) != 0

    @classmethod
    def to_block(cls, block, offset, value):
        if value:
            block.write_multi(offset, 1, cls.size)
        else:
            block.write_multi(offset, 0, cls.size)

    @classmethod
    def from_yml_rep(cls, yml_rep):
        if isinstance(yml_rep, bool):
            return yml_rep
        else:
            raise InvalidArgumentError("Could not parse value[{}] as a boolean".format(yml_rep))

    @classmethod
    def to_yml_rep(cls, value):
        return value


class LittleEndianIntegerTableEntry(object):
    @classmethod
    def from_block(cls, block, offset):
        return block.read_multi(offset, cls.size)

    @classmethod
    def to_block(cls, block, offset, value):
        block.write_multi(offset, value, cls.size)

    @classmethod
    def from_yml_rep(cls, yml_rep):
        if isinstance(yml_rep, int):
            return yml_rep
        else:
            raise InvalidArgumentError("Could not parse value[{}] of type[{}] as int".format(
                yml_rep, type(yml_rep).__name__))

    @classmethod
    def to_yml_rep(cls, value):
        return value


class LittleEndianHexIntegerTableEntry(LittleEndianIntegerTableEntry):
    pass


class LittleEndianOneBasedIntegerTableEntry(LittleEndianIntegerTableEntry):
    @classmethod
    def from_block(cls, block, offset):
        return super(LittleEndianOneBasedIntegerTableEntry, cls).from_block(block, offset) + 1

    @classmethod
    def to_block(cls, block, offset, value):
        super(LittleEndianOneBasedIntegerTableEntry, cls).to_block(block, offset, value - 1)


class EnumeratedLittleEndianIntegerTableEntry(LittleEndianIntegerTableEntry):
    @classmethod
    def from_yml_rep(cls, yml_rep):
        if isinstance(yml_rep, str):
            try:
                return cls.enumeration_class.fromstring(yml_rep)
            except InvalidArgumentError:
                raise InvalidArgumentError("Could not parse string[{}] to type[{}]".format(
                    yml_rep, cls.enumeration_class.__name__))
        elif isinstance(yml_rep, int):
            return super(EnumeratedLittleEndianIntegerTableEntry, cls).from_yml_rep(yml_rep)
        else:
            raise InvalidArgumentError("Could not parse value[{}] to type[{}]".format(
                yml_rep, cls.enumeration_class.__name__))

    @classmethod
    def to_yml_rep(cls, value):
        try:
            return cls.enumeration_class.tostring(value)
        except InvalidArgumentError:
            return value


class ByteListTableEntry(object):
    @classmethod
    def from_block(cls, block, offset):
        return block[offset:offset + cls.size].to_list()

    @classmethod
    def to_block(cls, block, offset, value):
        block[offset:offset + cls.size] = value

    @classmethod
    def from_yml_rep(cls, yml_rep):
        if isinstance(yml_rep, list) and all(isinstance(x, int) for x in yml_rep):
            return yml_rep
        else:
            raise InvalidArgumentError("Could not parse value[{}] of type[{}] to a list of ints".format(
                yml_rep, type(yml_rep).__name__))

    @classmethod
    def to_yml_rep(cls, value):
        return cls.from_yml_rep(value)


class BitfieldTableEntry(object):
    @classmethod
    def from_block(cls, block, offset):
        value = set()
        block_value = block.read_multi(offset, cls.size)
        for i in range(0, cls.size * 8):
            if (1 << i) & block_value != 0:
                value.add(i)
        return value

    @classmethod
    def to_block(cls, block, offset, value):
        block_value = 0
        for i in value:
            block_value |= (1 << i)
        block.write_multi(offset, block_value, cls.size)

    @classmethod
    def from_yml_rep(cls, yml_rep):
        if isinstance(yml_rep, list) and all((isinstance(x, int) or isinstance(x, str)) for x in yml_rep):
            value = set()
            for entry in yml_rep:
                if isinstance(entry, str):
                    try:
                        entry = cls.enumeration_class.fromstring(entry)
                    except InvalidArgumentError:
                        raise InvalidArgumentError("Could not parse string[{}] to type[{}]".format(
                            entry, cls.enumeration_class.__name__))

                if entry >= cls.size * 8:
                    raise InvalidArgumentError("Bitvalue value[{}] is too large to fit in a bitfield of size[{}]"
                                               .format(entry, cls.size))

                value.add(entry)
            return value
        else:
            raise InvalidArgumentError("Expected list of bitvalues but instead got value[{}] of type[{}]".format(
                yml_rep, type(yml_rep).__name__))

    @classmethod
    def to_yml_rep(cls, value):
        yml_rep = []
        for bitvalue in value:
            try:
                yml_rep.append(cls.enumeration_class.tostring(bitvalue))
            except InvalidArgumentError:
                yml_rep.append(bitvalue)
        yml_rep.sort()
        return yml_rep


class Table(object):
    def __init__(self, name="Anonymous Table", size=None, num_rows=None, schema_specification=None):
        self.name = name

        if size is None and num_rows is None:
            raise InvalidArgumentError("Cannot create table[{}] with null size and null num_rows".format(self.name))

        self.schema = map(self.to_table_entry_class, schema_specification)
        self.row_size = sum(map(lambda x: x.size, self.schema))
        self.row_length = len(self.schema)
        if num_rows is not None:
            self.num_rows = num_rows
        else:
            if size % self.row_size != 0:
                raise InvalidArgumentError("Cannot create table[{}] with rows of size[{}] and total size[{}]".format(
                    self.name, self.row_size, size))
            self.num_rows = size / self.row_size
        self.values = [[None] * len(self.schema) for i in range(self.num_rows)]

    def from_block(self, block, offset):
        for row in self.values:
            for j, column in enumerate(self.schema):
                row[j] = column.from_block(block, offset)
                offset += column.size

    def to_block(self, block, offset):
        original_offset = offset
        for row in self.values:
            for value, column in zip(row, self.schema):
                column.to_block(block, offset, value)
                offset += column.size
        return original_offset

    def from_yml_rep(self, yml_rep):
        for i, row in enumerate(self.values):
            yml_rep_row = yml_rep[i]
            for j, column in enumerate(self.schema):
                row[j] = column.from_yml_rep(yml_rep_row[column.name])

    def to_yml_rep(self):
        yml_rep = {}
        for i, row in enumerate(self.values):
            yml_rep_entry = {}
            for value, column in zip(row, self.schema):
                yml_rep_entry[column.name] = column.to_yml_rep(value)
            yml_rep[i] = yml_rep_entry
        return yml_rep

    def from_yml_file(self, f):
        yml_rep = yaml.load(f, yaml.CSafeLoader)
        self.from_yml_rep(yml_rep)

    def to_yml_file(self, f):
        yml_str_rep = yaml.dump(self.to_yml_rep(), default_flow_style=False, Dumper=yaml.CSafeDumper)

        # Rewrite hexints in hexidecimal
        # The YAML parser does not offer this option, so this has to be done with a regex
        for column in [x for x in self.schema if isinstance(x, LittleEndianHexIntegerTableEntry)]:
            yml_str_rep = re.sub("{}: (\d+)".format(re.escape(column["name"])),
                                 lambda i: "{}: {:#x}".format(column["name"],
                                                              int(i.group(0)[i.group(0).find(": ") + 2:])),
                                 yml_str_rep)

        f.write(yml_str_rep)

    def __getitem__(self, index):
        row, col = index
        if in_range(row, (0, self.num_rows)) and in_range(col, (0, self.row_length)):
            return self.values[row][col]
        else:
            raise IndexOutOfRangeError("Cannot get value at index[{},{}] from table of size[{},{}]".format(
                col, row, self.row_length, self.num_rows))

    def __setitem__(self, index, value):
        row, col = index
        if in_range(row, (0, self.num_rows)) and in_range(col, (0, self.row_length)):
            self.values[row][col] = value
        else:
            raise IndexOutOfRangeError("Cannot get value at index[{},{}] from table of size[{},{}]".format(
                col, row, self.row_length, self.num_rows))


class GenericLittleEndianTable(Table):
    DEFAULT_TABLE_ENTRY_TYPE = "int"
    TABLE_ENTRY_CLASS_MAP = {"int": (LittleEndianIntegerTableEntry, ["name", "size"]),
                             "hexint": (LittleEndianHexIntegerTableEntry, ["name", "size"]),
                             "one-based int": (LittleEndianOneBasedIntegerTableEntry, ["name", "size"]),
                             "bytearray": (ByteListTableEntry, ["name", "size"]),
                             "boolean": (BooleanTableEntry, ["name", "size"])}

    @classmethod
    def to_table_entry_class(cls, column_specification):
        class_name = "GeneratedTableEntry_{}".format(column_specification["name"])
        column_specification["size"] = getitem_with_default(column_specification, "size", 1)
        column_specification["type"] = getitem_with_default(column_specification, "type", cls.DEFAULT_TABLE_ENTRY_TYPE)
        if (column_specification["type"] == "int") and ("values" in column_specification):
            enumeration_class = type("{}_Enum".format(class_name),
                                     (GenericEnum,),
                                     dict(zip([str(x).upper() for x in column_specification["values"]],
                                              range(len(column_specification["values"])))))
            return type(class_name,
                        (EnumeratedLittleEndianIntegerTableEntry,),
                        {"name": column_specification["name"],
                         "size": column_specification["size"],
                         "enumeration_class": enumeration_class})
        elif (column_specification["type"] == "bitfield") and ("bitvalues" in column_specification):
            enumeration_class = type("{}_Enum".format(class_name),
                                     (GenericEnum,),
                                     dict(zip([str(x).upper() for x in column_specification["bitvalues"]],
                                              range(len(column_specification["bitvalues"])))))
            return type(class_name,
                        (BitfieldTableEntry,),
                        {"name": column_specification["name"],
                         "size": column_specification["size"],
                         "enumeration_class": enumeration_class})
        else:
            try:
                entry_class, parameter_list = cls.TABLE_ENTRY_CLASS_MAP[column_specification["type"]]
            except KeyError:
                raise InvalidArgumentError("Unknown table column type[{}]".format(column_specification["type"]))

            try:
                parameters = dict(map(lambda x: (x, column_specification[x]), parameter_list))
            except KeyError:
                raise InvalidArgumentError("Column[{}] in table schema not provided with all required attributes[{}]"
                                           .format(column_specification["name"], parameter_list))

            return type(class_name, (entry_class,), parameters)





