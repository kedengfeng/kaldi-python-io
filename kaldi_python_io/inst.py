#!/usr/bin/env python
# wujian@2018
"""
    Simple wrapper for _io_kernel.py
    - ArchiveReader
    - ScriptReader
    - ArchiveWriter
    - AlignArchiveReader
    - AlignScriptReader
    - Nnet3EgsReader
"""

import os
import sys
import glob
import random
import warnings
import _thread
import threading
import subprocess

from io import TextIOWrapper

import numpy as np
from . import _io_kernel as io

__all__ = [
    "ArchiveReader", "ScriptReader", "AlignArchiveReader", "AlignScriptReader",
    "ArchiveWriter", "Nnet3EgsReader", "Reader"
]


def pipe_fopen(command, mode, background=True):
    if mode not in ["rb", "r"]:
        raise RuntimeError("Now only support input from pipe")

    p = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE)

    def background_command_waiter(command, p):
        p.wait()
        if p.returncode != 0:
            warnings.warn("Command \"{0}\" exited with status {1}".format(
                command, p.returncode))
            _thread.interrupt_main()

    if background:
        thread = threading.Thread(
            target=background_command_waiter, args=(command, p))
        # exits abnormally if main thread is terminated .
        thread.daemon = True
        thread.start()
    else:
        background_command_waiter(command, p)
    return p.stdout


def _fopen(fname, mode):
    """
    Extend file open function, to support 
        1) "-", which means stdin/stdout
        2) "$cmd |" which means pipe.stdout
    """
    if mode not in ["w", "r", "wb", "rb"]:
        raise ValueError("Unknown open mode: {mode}".format(mode=mode))
    if not fname:
        return None
    fname = fname.rstrip()
    if fname == "-":
        if mode in ["w", "wb"]:
            return sys.stdout.buffer if mode == "wb" else sys.stdout
        else:
            return sys.stdin.buffer if mode == "rb" else sys.stdin
    elif fname[-1] == "|":
        pin = pipe_fopen(fname[:-1], mode, background=(mode == "rb"))
        return pin if mode == "rb" else TextIOWrapper(pin)
    else:
        if mode in ["r", "rb"] and not os.path.exists(fname):
            raise FileNotFoundError(
                "Could not find common file: {}".format(fname))
        return open(fname, mode)


def _fclose(fname, fd):
    """
    Extend file close function, to support
        1) "-", which means stdin/stdout
        2) "$cmd |" which means pipe.stdout
        3) None type
    """
    if fname != "-" and fd and fname[-1] != "|":
        fd.close()


class ext_open(object):
    """
    To make _fopen/_fclose easy to use like:
    with open("egs.scp", "r") as f:
        ...
    
    """

    def __init__(self, fname, mode):
        self.fname = fname
        self.mode = mode

    def __enter__(self):
        self.fd = _fopen(self.fname, self.mode)
        return self.fd

    def __exit__(self, *args):
        _fclose(self.fname, self.fd)


def parse_scps(scp_path, value_processor=lambda x: x, num_tokens=2):
    """
    Parse kaldi's script(.scp) file with supported for stdin
    WARN: last line of scripts could not be None and with "\n" end
    """
    scp_dict = dict()
    line = 0
    with ext_open(scp_path, "r") as f:
        for raw_line in f:
            scp_tokens = raw_line.strip().split()
            line += 1
            if num_tokens >= 2 and len(scp_tokens) != num_tokens or len(
                    scp_tokens) < 2:
                raise RuntimeError(
                    "For {}, format error in line[{:d}]: {}".format(
                        scp_path, line, raw_line))
            if num_tokens == 2:
                key, value = scp_tokens
            else:
                key, value = scp_tokens[0], scp_tokens[1:]
            if key in scp_dict:
                raise ValueError("Duplicate key \'{0}\' exists in {1}".format(
                    key, scp_path))
            scp_dict[key] = value_processor(value)
    return scp_dict


class Reader(object):
    """
        Base class for sequential/random accessing, to be implemented
    """

    def __init__(self, scp_path, value_processor=lambda x: x, num_tokens=2):
        self.index_dict = parse_scps(
            scp_path, value_processor=value_processor, num_tokens=num_tokens)
        self.index_keys = list(self.index_dict.keys())

    # return values
    def _load(self, key):
        return self.index_dict[key]

    # number of utterance
    def __len__(self):
        return len(self.index_dict)

    # avoid key error
    def __contains__(self, key):
        return key in self.index_dict

    # sequential index
    def __iter__(self):
        for key in self.index_keys:
            yield key, self._load(key)

    # random index, support str/int as index
    def __getitem__(self, index):
        if type(index) not in [int, str]:
            raise IndexError("Unsupported index type: {}".format(type(index)))
        if type(index) == int:
            # from int index to key
            num_utts = len(self.index_keys)
            if index >= num_utts or index < 0:
                raise KeyError(
                    "Interger index out of range, {:d} vs {:d}".format(
                        index, num_utts))
            index = self.index_keys[index]
        if index not in self.index_dict:
            raise KeyError("Missing utterance {}!".format(index))
        return self._load(index)


class SequentialReader(object):
    """
        Base class for sequential reader(only for .ark/.egs)
    """

    def __init__(self, ark_or_pipe):
        self.ark_or_pipe = ark_or_pipe

    def __iter__(self):
        raise NotImplementedError


class ScriptReader(Reader):
    """
        Reader for kaldi's scripts(for BaseFloat matrix)
    """

    def __init__(self, ark_scp, matrix=True):
        self.fmgr = dict()

        def addr_processor(addr):
            addr_token = addr.split(":")
            if len(addr_token) == 1:
                raise ValueError("Unsupported scripts address format")
            path, offset = ":".join(addr_token[0:-1]), int(addr_token[-1])
            return (path, offset)

        super(ScriptReader, self).__init__(
            ark_scp, value_processor=addr_processor)
        self.loadf = io.read_float_mat if matrix else io.read_float_vec

    def __del__(self):
        for name in self.fmgr:
            self.fmgr[name].close()

    def _open(self, obj, addr):
        if obj not in self.fmgr:
            self.fmgr[obj] = open(obj, "rb")
        arkf = self.fmgr[obj]
        arkf.seek(addr)
        return arkf

    def _load(self, key):
        path, addr = self.index_dict[key]
        fd = self._open(path, addr)
        io.expect_binary(fd)
        obj = self.loadf(fd)
        return obj


class Writer(object):
    """
        Base class, to be implemented
    """

    def __init__(self, ark_path, scp_path=None):
        self.scp_path = scp_path
        self.ark_path = ark_path
        # if dump ark to output, then ignore scp
        if ark_path == "-" and scp_path:
            warnings.warn(
                "Ignore .scp output discriptor cause dump archives to stdout")
            self.scp_path = None

    def __enter__(self):
        # "wb" is important
        self.ark_file = _fopen(self.ark_path, "wb")
        self.scp_file = _fopen(self.scp_path, "w")
        return self

    def __exit__(self, type, value, trace):
        _fclose(self.ark_path, self.ark_file)
        _fclose(self.scp_path, self.scp_file)

    def write(self, key, value):
        raise NotImplementedError


class ArchiveReader(SequentialReader):
    """
        Sequential Reader for Kalid's archive(.ark) object
    """

    def __init__(self, ark_or_pipe, matrix=True):
        super(ArchiveReader, self).__init__(ark_or_pipe)
        self.matrix = matrix

    def __iter__(self):
        with ext_open(self.ark_or_pipe, "rb") as fd:
            for key, mat in io.read_ark(fd, matrix=self.matrix):
                yield key, mat


class Nnet3EgsReader(SequentialReader):
    """
        Sequential Reader for Kalid's nnet3 .egs object
    """

    def __init__(self, ark_or_pipe):
        super(Nnet3EgsReader, self).__init__(ark_or_pipe)

    def __iter__(self):
        with ext_open(self.ark_or_pipe, "rb") as fd:
            for key, egs in io.read_nnet3_egs_ark(fd):
                yield key, egs


class AlignArchiveReader(SequentialReader):
    """
        Reader for kaldi's alignment archives
    """

    def __init__(self, ark_or_pipe):
        super(AlignArchiveReader, self).__init__(ark_or_pipe)

    def __iter__(self):
        with ext_open(self.ark_or_pipe, "rb") as fd:
            for key, ali in io.read_ali(fd):
                yield key, ali


class AlignScriptReader(ScriptReader):
    """
        Reader for kaldi's scripts(for int32 vector, such as alignments)
    """

    def __init__(self, ark_scp):
        super(AlignScriptReader, self).__init__(ark_scp)

    def _load(self, key):
        path, addr = self.index_dict[key]
        fd = self._open(path, addr)
        io.expect_binary(fd)
        obj = io.read_int_vec(fd)
        return obj


class ArchiveWriter(Writer):
    """
        Writer for kaldi's archive && scripts (for BaseFloat matrix)
    """

    def __init__(self, ark_path, scp_path=None, matrix=True):
        super(ArchiveWriter, self).__init__(ark_path, scp_path)
        self.dumpf = io.write_common_mat if matrix else io.write_float_vec

    def write(self, key, obj):
        io.write_token(self.ark_file, key)
        if self.ark_path != "-":
            offset = self.ark_file.tell()
        io.write_binary_symbol(self.ark_file)
        self.dumpf(self.ark_file, obj)
        if self.scp_file:
            record = "{0}\t{1}:{2}\n".format(key, os.path.abspath(
                self.ark_path), offset)
            self.scp_file.write(record)


def test_archive_writer(ark, scp):
    # for matrix
    with ArchiveWriter(ark, scp) as writer:
        for i in range(10):
            mat = np.random.rand(100, 20)
            writer.write("mat-{:d}".format(i), mat)
    scp_reader = ScriptReader(scp)
    for key, mat in scp_reader:
        print("{0}: {1}".format(key, mat.shape))
    # for vector
    with ArchiveWriter(ark, scp, matrix=False) as writer:
        for i in range(10):
            vec = np.random.rand(100)
            print(vec)
            writer.write("vec-{:d}".format(i), vec)
    scp_reader = ScriptReader(scp, matrix=False)
    for key, vec in scp_reader:
        print(vec)
        print("{0}: {1}".format(key, vec.size))
    print("TEST *test_archieve_writer* DONE!")


def test_archive_reader(ark_or_pipe, matrix=True):
    ark_reader = ArchiveReader(ark_or_pipe, matrix=matrix)
    for key, obj in ark_reader:
        print("{0}: {1}".format(key, obj.shape))
    print("TEST *test_archive_reader* DONE!")


def test_script_reader(scp, matrix=True):
    scp_reader = ScriptReader(scp, matrix=matrix)
    for key, obj in scp_reader:
        print("{0}: {1}".format(key, obj.shape))
    print("TEST *test_script_reader* DONE!")


def test_align_archive_reader(ark_or_pipe):
    ali_reader = AlignArchiveReader(ark_or_pipe)
    for key, vec in ali_reader:
        print("{0}: {1}".format(key, vec.shape))
    print("TEST *test_align_archive_reader* DONE!")


def test_nnet3egs_reader(egs):
    egs_reader = Nnet3EgsReader(egs)
    for key, _ in egs_reader:
        print("{}".format(key))
    print("TEST *test_nnet3egs_reader* DONE!")


if __name__ == "__main__":
    test_archive_writer("asset/foo.ark", "asset/foo.scp")
    # archive_reader
    test_archive_reader("asset/6.mat.ark", matrix=True)
    test_archive_reader("asset/6.vec.ark", matrix=False)
    test_archive_reader("copy-feats ark:asset/6.mat.ark ark:- |")
    # script_reader
    test_script_reader("asset/6.mat.scp", matrix=True)
    test_script_reader("asset/6.vec.scp", matrix=False)
    test_script_reader("shuf asset/6.mat.scp | head -n 2 |")
    # align_archive_reader
    test_align_archive_reader("gunzip -c asset/10.ali.gz |")
    # nnet3egs_reader
    test_nnet3egs_reader("asset/10.egs")
