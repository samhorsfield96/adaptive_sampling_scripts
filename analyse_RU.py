#!/usr/bin/env python

import sys, getopt, errno
import re
import mappy as mp
from collections import defaultdict
from Bio import SeqIO
from pathlib import Path
import argparse
import gzip
import os
from random import choices

def get_options():
	description = "Aligns and determines enrichment factor"
	parser = argparse.ArgumentParser(description=description,
									 prog='python analyse_RU.py')
	IO = parser.add_argument_group('Input/options.out')
	IO.add_argument('-f',
					required=True,
					help='Input directory containing fastq files.')
	IO.add_argument('-i',
					required=True,
					help='Reference for minimap2 alignment (can be fasta or mmi).')
	IO.add_argument('-c',
					default="1-256",
					help='channels to separate, in the form \"a-b\". '
						 '[Default=1-256]')
	IO.add_argument('-p',
					default=0.8,
					type=float,
					help='Minimum proportion of bases matching reference in alignment. '
						 'Default=0.8')
	IO.add_argument('-o',
					default="RU_output",
					help='Output prefix. '
						 'Default="RU_output"')
	IO.add_argument('-r',
					default=False,
					action="store_true",
					help='Remove multi-mapping reads.'
						 'Default=False')
	IO.add_argument('-b',
					default=False,
					action="store_true",
					help='Align only pass reads.'
						 'Default=False')
	IO.add_argument('-t',
					default=None,
					help='Specify targets within minimap2 index with comma separated list. Default = None ')
	IO.add_argument('-q',
					default=False,
					action="store_true",
					help='Generate fastq files for aligned sequence. '
						 'Default=False')
	IO.add_argument('-v',
					default=False,
					action="store_true",
					help='Verbose output.'
						 'Default=False')
	IO.add_argument('-bs',
					default=100,
					type=int,
					help='Bootstrap sample size.'
						 'Default=100')
	return parser.parse_args()

class ReferenceStats:
	def __init__(self, reference):
		self.totalReads = 0
		self.totalLength = 0
		self.reference = reference

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

def default_val():
    return (None, None, 0, 0, 0)

# determine best loci alignment in each reference
def get_best_map(index, fasta, cutoff=0.7):
    a = mp.Aligner(index, preset="asm10")

    ref_dict = defaultdict(default_val)

    fasta_sequences = SeqIO.parse(open(fasta), 'fasta')
    for fasta in fasta_sequences:
        id, sequence = fasta.id, str(fasta.seq)

        for hit in a.map(sequence):
            query_hit = hit.blen

            # set cutoff for minimum alignment length
            if query_hit < cutoff * len(sequence):
                continue

            if query_hit > ref_dict[hit.ctg][-1]:
                ref_dict[hit.ctg] = (id, hit.r_st, hit.r_en, query_hit)

    return ref_dict

def main():
	options = get_options()

	fastqDir = options.f
	reference = options.i
	channels = options.c
	fields = channels.split("-")
	min_channel = int(fields[0])
	max_channel = int(fields[1])
	matching_prop = options.p
	output = options.o
	remove_multi = options.r
	only_pass = options.b
	verbose = options.v
	gen_fastq = options.q
	mm_target = options.t
	bootstrap_sample = options.bs

	# split list of targets
	if mm_target != None:
		mm_target = set(mm_target.split(","))

	# initialise results dictionaries
	results_dict = {}
	read_seqs = {"target" : {},
				 "non-target" : {}}
	read_lens = {}

	mapper = mp.Aligner(reference, preset="map-ont")

	# keep list of barcodes to ensure printing in order
	barcode_list = []

	for f in get_fq(fastqDir):
		if only_pass:
			if "/fastq_pass/" not in f:
				continue

		# only run on gzipped files
		if ".gz" not in f:
			continue

		if verbose:
			print("Aligning: {}".format(str(f)))
		# get filename and extension
		base = os.path.splitext(os.path.basename(f))[0].split("_")
		# print(base)
		if "barcode" in base[2]:
			barcode = base[2]
		else:
			barcode = "NA"

		if barcode not in results_dict:
			results_dict[barcode] = {"target_channel_bases": 0,
									 "non_target_channel_bases" : 0,
									 "target_channel_reads" : 0,
									 "non_target_channel_reads" : 0,
									 "target_channel_bases_mapped": 0,
									 "non_target_channel_bases_mapped": 0,
									 "target_channel_reads_mapped": 0,
									 "non_target_channel_reads_mapped": 0,
									 "total_bases" : 0,
									 "total_reads" : 0,
									 "ref_dict" : {}}
			read_seqs["target"][barcode] = {}
			read_seqs["non-target"][barcode] = {}
			read_lens[barcode] = {}
			read_lens[barcode]["adaptive"] = []
			read_lens[barcode]["control"] = []

			barcode_list.append(barcode)

		with gzip.open(f, "rt") as handle:
			input_sequences = SeqIO.parse(handle, 'fastq')
			for entry in input_sequences:
				fields, seq = entry.description, str(entry.seq)
				fields = fields.split()
				readName = fields[0][1:]
				channel = int(fields[3].split("=")[1])
				assert (fields[3].split("=")[0] == "ch")
				assert (channel >= 0 and channel <= 512)


				# determine is read is in target channel
				target = False
				if channel >= min_channel and channel <= max_channel:
					target = True

				# read_lengths[readName] = length
				# read_seqs[readName] = seq

				ref_align = "unaligned"

				# perc id is alignment coverage of reference, just in case clipping present
				perc_id = 0
				match_len = 0

				align_count = 0

				# Map seq, take alignment with largest matching sequence only
				for r in mapper.map(seq):
					align_count += 1

					len_ref_map = abs(r.r_en - r.r_st)

					# take as best alignment if matching length is longer
					if mm_target is None:
						if match_len < r.mlen:
							match_len = r.mlen
							ref_align = r.ctg
							perc_id = match_len / len_ref_map
					# otherwise make sure read aligns to known target
					else:
						if r.ctg in mm_target:
							if match_len < r.mlen:
								match_len = r.mlen
								ref_align = r.ctg
								perc_id = match_len / len_ref_map

				# if below cutoff, or removing multialigning reads, set reference as unaligned
				if perc_id < matching_prop:
					ref_align = "unaligned"
				if remove_multi and align_count > 1:
					ref_align = "unaligned"


				# take length as alignment length
				total_length = len(seq)
				if ref_align == "unaligned":
					match_len = total_length

				# take length as alignment length
				if target:
					read_lens[barcode]["adaptive"].append((ref_align, match_len, total_length))
				else:
					read_lens[barcode]["control"].append((ref_align, match_len, total_length))

				# add to read_seqs
				if gen_fastq:
					if ref_align not in read_seqs["target"][barcode]:
						read_seqs["target"][barcode][ref_align] = []
						read_seqs["non-target"][barcode][ref_align] = []

					if target:
						read_seqs["target"][barcode][ref_align].append((readName, perc_id, seq))
					else:
						read_seqs["non-target"][barcode][ref_align].append((readName, perc_id, seq))

				if ref_align not in results_dict[barcode]["ref_dict"]:
					results_dict[barcode]["ref_dict"][ref_align] = {"target_channel_bases": 0,
																	"non_target_channel_bases" : 0,
																	"target_channel_reads" : 0,
																	"non_target_channel_reads" : 0}

				if target:
					results_dict[barcode]["target_channel_bases"] += total_length
					results_dict[barcode]["target_channel_reads"] += 1
					results_dict[barcode]["ref_dict"][ref_align]["target_channel_bases"] += match_len
					results_dict[barcode]["ref_dict"][ref_align]["target_channel_reads"] += 1
					if ref_align != "unaligned":
						results_dict[barcode]["target_channel_bases_mapped"] += match_len
						results_dict[barcode]["target_channel_reads_mapped"] += 1
				else:
					results_dict[barcode]["non_target_channel_bases"] += total_length
					results_dict[barcode]["non_target_channel_reads"] += 1
					results_dict[barcode]["ref_dict"][ref_align]["non_target_channel_bases"] += match_len
					results_dict[barcode]["ref_dict"][ref_align]["non_target_channel_reads"] += 1
					if ref_align != "unaligned":
						results_dict[barcode]["non_target_channel_bases_mapped"] += match_len
						results_dict[barcode]["non_target_channel_reads_mapped"] += 1

				results_dict[barcode]["total_bases"] += total_length
				results_dict[barcode]["total_reads"] += 1

	# write bootstrapped sample file
	bootstrapped_enrichment = {}
	for barcode, channel_dict in read_lens.items():
		bootstrapped_enrichment[barcode] = {}

		adaptive_lens = channel_dict["adaptive"]
		control_lens = channel_dict["control"]

		# sample with replacement full dataset
		for i in range(bootstrap_sample):
			#print(i)
			adaptive_num_bases = {}
			control_num_bases = {}
			adaptive_sample = choices(adaptive_lens, k=len(adaptive_lens))
			control_sample = choices(control_lens, k=len(control_lens))

			adaptive_total_bases = 0
			control_total_bases = 0

			for read in adaptive_sample:
				align, match_len, read_len = read
				if align not in adaptive_num_bases:
					adaptive_num_bases[align] = 0
					control_num_bases[align] = 0
				adaptive_num_bases[align] += match_len
				adaptive_total_bases += read_len

			for read in control_sample:
				align, match_len, read_len = read
				if align not in adaptive_num_bases:
					adaptive_num_bases[align] = 0
					control_num_bases[align] = 0
				control_num_bases[align] += match_len
				control_total_bases += read_len

			# get bootstrap enrichment
			for align in adaptive_num_bases.keys():
				if align not in bootstrapped_enrichment[barcode]:
					bootstrapped_enrichment[barcode][align] = []

				# add one to avoid infinite value
				if control_num_bases[align] == 0:
					control_num_bases[align] = 1
				if control_total_bases == 0:
					control_total_bases = 1

				if adaptive_total_bases == 0:
					bootstrapped_enrichment[barcode][align].append(0)
				else:
					#adaptive_prop = adaptive_num_bases[align] / adaptive_total_bases
					#control_prop = control_num_bases[align] / control_total_bases
					bootstrapped_enrichment[barcode][align].append((adaptive_num_bases[align] / adaptive_total_bases) /
																   (control_num_bases[align] / control_total_bases))

	with open(output + "_bootstrap.txt", "w") as o_boot:
		o_boot.write("Barcode\tAlignment\tEnrichment\n")
		for barcode, align_dict in bootstrapped_enrichment.items():
			for align, len_list in align_dict.items():
				for length in len_list:
					o_boot.write("{}\t{}\t{}\n".format(barcode, align, length))

	# write summary file
	with open(output + "_summary.txt", "w") as o_sum:
		o_sum.write("Statistic\tChannel\tBarcode\tAlignment\tValue\n")
		# create dictionary to determine enrichment
		enrichment_dict = {}

		# target channels
		if verbose:
			print("Reference stats for channels " + str(channels) + ": ")
		for barcode in barcode_list:
			enrichment_dict[barcode] = {}
			if verbose:
				print("Target Barcode: " + str(barcode))
				print("Total number of reads mapped: " + str(results_dict[barcode]["target_channel_reads_mapped"]) + "/" + str(results_dict[barcode]["target_channel_reads"]))
				print("Total read bases: " + str(results_dict[barcode]["target_channel_bases"]))
				print("Total read bases mapped: " + str(results_dict[barcode]["target_channel_bases_mapped"]))
			for ref, item in results_dict[barcode]["ref_dict"].items():
				if verbose:
					print(ref + "\t" + str(item["target_channel_reads"]) + "\t" + str(item["target_channel_bases"]))
				o_sum.write("Reads_mapped\t{}\t{}\t{}\t{}\n".format("Target", str(barcode), ref, str(item["target_channel_reads"])))
				o_sum.write("Bases_mapped\t{}\t{}\t{}\t{}\n".format("Target", str(barcode), ref, str(item["target_channel_bases"])))
				enrichment_dict[barcode][ref] = {}
				if results_dict[barcode]["target_channel_bases"] == 0:
					enrichment_dict[barcode][ref]["Target_prop_bases"] = 0
				else:
					enrichment_dict[barcode][ref]["Target_prop_bases"] = item["target_channel_bases"] / results_dict[barcode]["target_channel_bases"]

			if gen_fastq:
				for ref, read_list in read_seqs["target"][barcode].items():
					if len(read_list) < 1:
						continue
					with open(output + "_" + str(barcode) + "_target_" + str(ref) + ".fasta", "w") as o:
						for read_tup in read_list:
							read_id, identity, seq = read_tup
							o.write(">" + read_id + "\t" + ref + "\t" + str(identity) + "\n" + seq + "\n")

			# write to summary file
			o_sum.write("Reads_total\t{}\t{}\t{}\t{}\n".format("Target", str(barcode), "Total", str(results_dict[barcode]["target_channel_reads"])))
			o_sum.write("Reads_mapped\t{}\t{}\t{}\t{}\n".format("Target", str(barcode), "Total", str(results_dict[barcode]["target_channel_reads_mapped"])))
			o_sum.write("Bases_total\t{}\t{}\t{}\t{}\n".format("Target", str(barcode), "Total", str(results_dict[barcode]["target_channel_bases"])))
			o_sum.write("Bases_mapped\t{}\t{}\t{}\t{}\n".format("Target", str(barcode), "Total", str(results_dict[barcode]["target_channel_bases_mapped"])))

		# non target channels
		if verbose:
			print("\nReference stats for all other channels: ")
		for barcode in barcode_list:
			if verbose:
				print("Non-target Barcode: " + str(barcode))
				print("Total number of reads mapped: " + str(results_dict[barcode]["non_target_channel_reads_mapped"]) + "/" + str(results_dict[barcode]["non_target_channel_reads"]))
				print("Total read bases: " + str(results_dict[barcode]["non_target_channel_bases"]))
				print("Total read bases mapped: " + str(results_dict[barcode]["non_target_channel_bases_mapped"]))
			for ref, item in results_dict[barcode]["ref_dict"].items():
				if verbose:
					print(ref + "\t" + str(item["non_target_channel_reads"]) + "\t" + str(item["non_target_channel_bases"]))
				o_sum.write("Reads_mapped\t{}\t{}\t{}\t{}\n".format("Non-target", str(barcode), ref, str(item["non_target_channel_reads"])))
				o_sum.write("Bases_mapped\t{}\t{}\t{}\t{}\n".format("Non-target", str(barcode), ref, str(item["non_target_channel_bases"])))
				if ref not in enrichment_dict[barcode]:
					enrichment_dict[barcode][ref] = {}
				enrichment_dict[barcode][ref]["Nontarget_bases_mapped"] = item["non_target_channel_bases"]

			if gen_fastq:
				for ref, read_list in read_seqs["non-target"][barcode].items():
					if len(read_list) < 1:
						continue
					with open(output + "_" + str(barcode) + "_nontarget_" + str(ref) + ".fasta", "w") as o:
						for read_tup in read_list:
							read_id, identity, seq = read_tup
							o.write(">" + read_id + "\t" + ref + "\t" + str(identity) + "\n" + seq + "\n")

			# calculate enrichment for all entries with mappings in target and non-target
			for ref in enrichment_dict[barcode].keys():
				if "Nontarget_bases_mapped" in enrichment_dict[barcode][ref] and "Target_prop_bases" in enrichment_dict[barcode][ref]:
					if enrichment_dict[barcode][ref]["Nontarget_bases_mapped"] == 0:
						enrichment_dict[barcode][ref]["Nontarget_bases_mapped"] = 1
					if results_dict[barcode]["non_target_channel_bases"] == 0:
						results_dict[barcode]["non_target_channel_bases"] = 1

					non_target_prop_bases = enrichment_dict[barcode][ref]["Nontarget_bases_mapped"] / results_dict[barcode]["non_target_channel_bases"]
					enrichment = enrichment_dict[barcode][ref]["Target_prop_bases"] / non_target_prop_bases

					o_sum.write("Enrichment\t{}\t{}\t{}\t{}\n".format("NA", str(barcode), ref, str(enrichment)))

			# write to summary file
			o_sum.write("Reads_total\t{}\t{}\t{}\t{}\n".format("Non-target", str(barcode), "Total", str(results_dict[barcode]["non_target_channel_reads"])))
			o_sum.write("Reads_mapped\t{}\t{}\t{}\t{}\n".format("Non-target", str(barcode), "Total", str(results_dict[barcode]["non_target_channel_reads_mapped"])))
			o_sum.write("Bases_total\t{}\t{}\t{}\t{}\n".format("Non-target", str(barcode), "Total", str(results_dict[barcode]["non_target_channel_bases"])))
			o_sum.write("Bases_mapped\t{}\t{}\t{}\t{}\n".format("Non-target", str(barcode), "Total", str(results_dict[barcode]["non_target_channel_bases_mapped"])))

if __name__ == "__main__":
    main()