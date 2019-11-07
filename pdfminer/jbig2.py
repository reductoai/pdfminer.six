import math
import os
from struct import pack, unpack, calcsize

# segment structure base
SEG_STRUCT = [
    (">L", "number"),
    (">B", "flags"),
    (">B", "retention_flags"),
    (">B", "page_assoc"),
    (">L", "data_length"),
]

# segment header literals
HEADER_FLAG_DEFERRED = 0b10000000
HEADER_FLAG_PAGE_ASSOC_LONG = 0b01000000

SEG_TYPE_MASK = 0b00111111

REF_COUNT_SHORT_MASK = 0b11100000
REF_COUNT_LONG_MASK = 0x1fffffff
REF_COUNT_LONG = 7

DATA_LEN_UNKNOWN = 0xffffffff

# segment types
SEG_TYPE_IMMEDIATE_GEN_REGION = 38
SEG_TYPE_END_OF_PAGE = 49
SEG_TYPE_END_OF_FILE = 50

# file literals
FILE_HEADER_ID = b'\x97\x4A\x42\x32\x0D\x0A\x1A\x0A'
FILE_HEAD_FLAG_SEQUENTIAL = 0b00000001
FILE_HEAD_FLAG_PAGES_UNKNOWN = 0b00000010


def bit_set(bit_pos, value):
    return bool((value >> bit_pos) & 1)


def check_flag(flag, value):
    return bool(flag & value)


def masked_value(mask, value):
    for bit_pos in range(0, 31):
        if bit_set(bit_pos, mask):
            return (value & mask) >> bit_pos

    raise Exception("Invalid mask or value")


def mask_value(mask, value):
    for bit_pos in range(0, 31):
        if bit_set(bit_pos, mask):
            return (value & (mask >> bit_pos)) << bit_pos

    raise Exception("Invalid mask or value")


class JBIG2StreamReader(object):
    """Read segments from a JBIG2 byte stream"""

    def __init__(self, stream):
        self.stream = stream

    def get_segments(self):
        segments = []
        while not self.is_eof():
            segment = {}
            for field_format, name in SEG_STRUCT:
                field_len = calcsize(field_format)
                field = self.stream.read(field_len)
                if len(field) < field_len:
                    segment["_error"] = True
                    break
                value = unpack(field_format, field)
                if len(value) == 1:
                    [value] = value
                parser = getattr(self, "parse_%s" % name, None)
                if callable(parser):
                    value = parser(segment, value, field)
                segment[name] = value

            if not segment.get("_error"):
                segments.append(segment)
        return segments

    def is_eof(self):
        if self.stream.read(1) == b'':
            return True
        else:
            self.stream.seek(-1, os.SEEK_CUR)
            return False

    def parse_flags(self, segment, flags, field):
        return {
            "deferred": check_flag(HEADER_FLAG_DEFERRED, flags),
            "page_assoc_long": check_flag(HEADER_FLAG_PAGE_ASSOC_LONG, flags),
            "type": masked_value(SEG_TYPE_MASK, flags)
        }

    def parse_retention_flags(self, segment, flags, field):
        ref_count = masked_value(REF_COUNT_SHORT_MASK, flags)
        retain_segments = []
        ref_segments = []

        if ref_count < REF_COUNT_LONG:
            for bit_pos in range(5):
                retain_segments.append(bit_set(bit_pos, flags))
        else:
            field += self.stream.read(3)
            [ref_count] = unpack(">L", field)
            ref_count = masked_value(REF_COUNT_LONG_MASK, ref_count)
            ret_bytes_count = int(math.ceil((ref_count + 1) / 8))
            for ret_byte_index in range(ret_bytes_count):
                [ret_byte] = unpack(">B", self.stream.read(1))
                for bit_pos in range(7):
                    retain_segments.append(bit_set(bit_pos, ret_byte))

        seg_num = segment["number"]
        if seg_num <= 256:
            ref_format = ">B"
        elif seg_num <= 65536:
            ref_format = ">I"
        else:
            ref_format = ">L"

        ref_size = calcsize(ref_format)

        for ref_index in range(ref_count):
            ref = self.stream.read(ref_size)
            [ref] = unpack(ref_format, ref)
            ref_segments.append(ref)

        return {
            "ref_count": ref_count,
            "retain_segments": retain_segments,
            "ref_segments": ref_segments,
        }

    def parse_page_assoc(self, segment, page, field):
        if segment["flags"]["page_assoc_long"]:
            field += self.stream.read(3)
            [page] = unpack(">L", field)
        return page

    def parse_data_length(self, segment, length, field):
        if length:
            if (segment["flags"]["type"] == SEG_TYPE_IMMEDIATE_GEN_REGION) \
                    and (length == DATA_LEN_UNKNOWN):

                raise NotImplementedError(
                    "Working with unknown segment length "
                    "is not implemented yet"
                )
            else:
                segment["raw_data"] = self.stream.read(length)

        return length


class JBIG2StreamWriter(object):
    """Write JBIG2 segments to a file in JBIG2 format"""

    def __init__(self, stream):
        self.stream = stream

    def write_segments(self, segments, fix_last_page=True):
        data_len = 0
        current_page = None
        seg_num = None

        for segment in segments:
            data = self.encode_segment(segment)
            self.stream.write(data)
            data_len += len(data)

            seg_num = segment["number"]

            if fix_last_page:
                seg_page = segment.get("page_assoc")

                if segment["flags"]["type"] == SEG_TYPE_END_OF_PAGE:
                    current_page = None
                elif seg_page:
                    current_page = seg_page

        if fix_last_page and current_page and (seg_num is not None):
            segment = self.get_eop_segment(seg_num + 1, current_page)
            data = self.encode_segment(segment)
            self.stream.write(data)
            data_len += len(data)

        return data_len

    def write_file(self, segments, fix_last_page=True):
        header = FILE_HEADER_ID
        header_flags = FILE_HEAD_FLAG_SEQUENTIAL | FILE_HEAD_FLAG_PAGES_UNKNOWN
        header += pack(">B", header_flags)
        self.stream.write(header)
        data_len = len(header)

        data_len += self.write_segments(segments, fix_last_page)

        seg_num = 0
        for segment in segments:
            seg_num = segment["number"]

        eof_segment = self.get_eof_segment(seg_num + 1)
        data = self.encode_segment(eof_segment)

        self.stream.write(data)
        data_len += len(data)

        return data_len

    def encode_segment(self, segment):
        data = b''
        for field_format, name in SEG_STRUCT:
            value = segment.get(name)
            encoder = getattr(self, "encode_%s" % name, None)
            if callable(encoder):
                field = encoder(value, segment)
            else:
                field = pack(field_format, value)
            data += field
        return data

    def encode_flags(self, value, segment):
        flags = 0
        if value.get("deferred"):
            flags |= HEADER_FLAG_DEFERRED

        if "page_assoc_long" in value:
            flags |= HEADER_FLAG_PAGE_ASSOC_LONG \
                if value["page_assoc_long"] else flags
        else:
            flags |= HEADER_FLAG_PAGE_ASSOC_LONG \
                if segment.get("page", 0) > 255 else flags

        flags |= mask_value(SEG_TYPE_MASK, value["type"])

        return pack(">B", flags)

    def encode_retention_flags(self, value, segment):
        flags = []
        flags_format = ">B"
        ref_count = value["ref_count"]
        retain_segments = value.get("retain_segments", [])

        if ref_count <= 4:
            flags_byte = mask_value(REF_COUNT_SHORT_MASK, ref_count)
            for ref_index, ref_retain in enumerate(retain_segments):
                flags_byte |= 1 << ref_index
            flags.append(flags_byte)
        else:
            bytes_count = math.ceil((ref_count + 1) / 8)
            flags_format = ">L" + ("B" * bytes_count)
            flags_dword = mask_value(
                REF_COUNT_SHORT_MASK,
                REF_COUNT_LONG
            ) << 24
            flags.append(flags_dword)

            for byte_index in range(bytes_count):
                ret_byte = 0
                ret_part = retain_segments[byte_index * 8:byte_index * 8 + 8]
                for bit_pos, ret_seg in enumerate(ret_part):
                    ret_byte |= 1 << bit_pos if ret_seg else ret_byte

                flags.append(ret_byte)

        ref_segments = value.get("ref_segments", [])

        seg_num = segment["number"]
        if seg_num <= 256:
            ref_format = "B"
        elif seg_num <= 65536:
            ref_format = "I"
        else:
            ref_format = "L"

        for ref in ref_segments:
            flags_format += ref_format
            flags.append(ref)

        return pack(flags_format, *flags)

    def encode_data_length(self, value, segment):
        data = pack(">L", value)
        data += segment["raw_data"]
        return data

    def get_eop_segment(self, seg_number, page_number):
        return {
            'data_length': 0,
            'flags': {'deferred': False, 'type': SEG_TYPE_END_OF_PAGE},
            'number': seg_number,
            'page_assoc': page_number,
            'raw_data': b'',
            'retention_flags': {
                'ref_count': 0,
                'ref_segments': [],
                'retain_segments': []
            }
        }

    def get_eof_segment(self, seg_number):
        return {
            'data_length': 0,
            'flags': {'deferred': False, 'type': SEG_TYPE_END_OF_FILE},
            'number': seg_number,
            'page_assoc': 0,
            'raw_data': b'',
            'retention_flags': {
                'ref_count': 0,
                'ref_segments': [],
                'retain_segments': []
            }
        }
