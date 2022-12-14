"""
Adapted from summarise_fq.py (https://github.com/LooseLab/readfish/blob/master/ru/summarise_fq.py)
"""
import gzip
from pathlib import Path
from statistics import mean, median, stdev
from collections import defaultdict
import sys
import os, glob
import mappy as mp

import argparse

def get_options():
    description = "Parses reads based on unblocks"
    parser = argparse.ArgumentParser(description=description,
                                     prog='python analyse_unblocks.py')
    IO = parser.add_argument_group('Input/options.out')
    IO.add_argument('--indir',
                    required=True,
                    help='Path to read directory')
    IO.add_argument('--unblocks',
                    default=None,
                    help='Path to unblocks file. Default is inferred from --indir.')
    IO.add_argument('--summary',
                    default=None,
                    help='Path to run summary file. Default is inferred from --indir.')
    IO.add_argument('--mux-period',
                    type=int,
                    default=480,
                    help='Period of mux scan (in seconds) to ignore reads prior. Default = 480 (8 mins)')
    IO.add_argument('--out',
                    default="result.txt",
                    help='Output file.')
    IO.add_argument('--ref',
                    default=None,
                    help='Specify minimap2 index. No alignment done if not specified. ')
    return parser.parse_args()

def readfq(fp):  # this is a generator function
    """Read FASTA/Q records from file handle
    https://github.com/lh3/readfq/blob/091bc699beee3013491268890cc3a7cbf995435b/readfq.py
    """
    last = None  # this is a buffer keeping the last unprocessed line
    while True:  # mimic closure; is it a bad idea?
        if not last:  # the first record or a record following a fastq
            for l in fp:  # search for the start of the next record
                if l[0] in ">@":  # fasta/q header line
                    last = l[:-1]  # save this line
                    break
        if not last:
            break
        name, seqs, last = last[1:].partition(" ")[0], [], None
        for l in fp:  # read the sequence
            if l[0] in "@+>":
                last = l[:-1]
                break
            seqs.append(l[:-1])
        if not last or last[0] != "+":  # this is a fasta record
            yield name, "".join(seqs), None  # yield a fasta record
            if not last:
                break
        else:  # this is a fastq record
            seq, leng, seqs = "".join(seqs), 0, []
            for l in fp:  # read the quality
                seqs.append(l[:-1])
                leng += len(l) - 1
                if leng >= len(seq):  # have read enough quality
                    last = None
                    yield name, seq, "".join(seqs)
                    # yield a fastq record
                    break
            if last:  # reach EOF before reading enough quality
                yield name, seq, None  # yield a fasta record instead
                break


def get_fq(directory):
    types = ([".fastq"], [".fastq", ".gz"], [".fq"], [".fq", ".gz"])
    files = (
        str(p.resolve()) for p in Path(directory).glob("**/*") if p.suffixes in types
    )
    yield from files


def main():
    options = get_options()
    reference = options.ref
    out = options.out
    indir = options.indir
    unblocks = options.unblocks
    summary = options.summary
    mux_period = options.mux_period

    if unblocks is None:
        unblocks = os.path.join(indir, "unblocked_read_ids.txt")

    if summary is None:
        sum_list = glob.glob(os.path.join(indir, "sequencing_summary_*.txt"))
        summary = sum_list[0]

    # create unblocks set
    unblock_set = set()
    with open(unblocks, "r") as f:
        for line in f:
            unblock_set.add(line.strip())
    print("Total unblocks: {}".format(len(unblock_set)))

    # create mux-period set
    mux_set = set()
    with open(summary, "r") as f:
        # ignore header
        next(f)
        for line in f:
            entry = line.strip().split("\t")
            read_id = entry[4]
            start_time = float(entry[9])
            if start_time < mux_period:
                mux_set.add(read_id)

    if reference is not None:
        mapper = mp.Aligner(reference, preset="map-ont")

        print("Using reference: {}".format(reference), file=sys.stderr)

    target_reads_dict = defaultdict(lambda: defaultdict(list))
    unblocks_reads_dict = defaultdict(lambda: defaultdict(list))

    for f in get_fq(indir):
        if f.endswith(".gz"):
            fopen = gzip.open
        else:
            fopen = open

        # get filename and extension
        base = os.path.splitext(os.path.basename(f))[0].split("_")
        #print(base)
        if "barcode" in base[2]:
            file_id = "_".join([base[1], base[2]])
        else:
            file_id = "_".join([base[1], "NA"])

        with fopen(f, "rt") as fh:
            for name, seq, _ in readfq(fh):
                ref = "None"
                if reference is not None:
                    # Map seq, only use first mapping (a bit janky)
                    for r in mapper.map(seq):
                        ref = r.ctg
                        break

                # check if in mux-period
                mux = 0
                if name in mux_set:
                    mux = 1

                if name in unblock_set:
                    unblocks_reads_dict[file_id][ref].append((name, len(seq), mux))
                else:
                    target_reads_dict[file_id][ref].append((name, len(seq), mux))
                    #print(name)

    with open(out, "w") as o:
        o.write("Type\tFilter\tBarcode\tRef\tLength\tName\tMux\n")
        for file_id, entry in target_reads_dict.items():
            type = file_id.split("_")
            for ref, length_list in entry.items():
                for len_entry in length_list:
                    o.write("Target\t" + type[0] + "\t" + type[1] + "\t" + ref + "\t" + str(len_entry[1])
                            + "\t" + len_entry[0] + "\t" + str(len_entry[2]) + "\n")
        for file_id, entry in unblocks_reads_dict.items():
            type = file_id.split("_")
            for ref, length_list in entry.items():
                for len_entry in length_list:
                    o.write("Non-target\t" + type[0] + "\t" + type[1] + "\t" + ref + "\t" + str(len_entry[1])
                            + "\t" + len_entry[0] + "\t" + str(len_entry[2]) + "\n")


if __name__ == "__main__":
    main()
