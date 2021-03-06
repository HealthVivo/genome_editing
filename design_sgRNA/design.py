"""Design sgRNAs for CRISPR/Cas9 Knock-out gene editing
Reference Genome: igenome UCSC hg19, start is 0-based
"""
import os
import numpy as np
import pandas as pd
import regex
import sqlalchemy
from Bio.Alphabet import IUPAC
from Bio.Seq import Seq

from genome_editing.score_sgrna.rs2 import compute_rs2
import genome_editing.score_sgrna.deep_rank as deep_rank
import genome_editing.score_sgrna.off_targets as off_targets
from genome_editing.score_sgrna.off_targets import sgrna_off_targets
from ..utils import alignment
import genome_editing.utils.utilities as util

GENOME_EDITING_URI = os.environ.get('GENOME_EDITING_URI')
CHROMS = ['chr' + str(x) for x in range(1, 23)]
CHROMS += ['chrX', 'chrY', 'chrM']


class Designer:
    """Design sgRNAs for a target, CRISPR/Cas9 knock-out"""

    def __init__(self, gene_symbol=None, refseq_id=None,
                 ref_genome='hg38',
                 sgrna_upstream=4, sgrna_downstream=7,
                 sgrna_length=20, flank=30, overlapped=True,
                 filter_tttt=False):
        """

        Args:
            gene_symbol: official gene symbol
            refseq_id: refseq ID
            sgrna_upstream: the length of upstream base pairs
            sgrna_downstream: the length of downstream base pairs
            sgrna_length: the length of sgRNA
            flank: the length of flank aroung each exon
            overlapped: whether find overlopped sgRNAs
            filter_tttt: whether filter sgRNAs containing TTTT
        """
        assert ref_genome in ('hg19', 'hg38', 'mm10'), 'Wrong reference genome'

        if refseq_id is not None:
            self.target_gene = Transcript(refseq_id, ref_genome=ref_genome)
            self.target_gene.get_sequence(flank)
        elif gene_symbol is not None:
            self.target_gene = Gene(gene_symbol.upper(), ref_genome=ref_genome)
            self.target_gene.get_sequence(flank)
        # else:
        #     raise BaseException('Error: please provide either gene symbol or'
        #                         'refseq ID')

        self.sgrna_upstream = sgrna_upstream
        self.sgrna_downstream = sgrna_downstream
        self.sgrna_length = sgrna_length
        self.flank = flank
        self.sgrnas = []
        self.overlapped = overlapped
        self.filter_tttt = filter_tttt

    def __repr__(self):
        return self.target_gene.gene_symbol

    def get_sgrnas(self, pams=('NGG', 'NAG')):
        """Get sgRNAs targeting input genes or transcript

        Args:
            pams: the pattern of PAM

        Returns:
            None, update self.sgrnas, which contain a list of SgRNA objects
        """
        exon_num = self.target_gene.exons.shape[0]
        cds_start = self.target_gene.cds_start[0]
        cds_end = self.target_gene.cds_end[0]

        for i in range(exon_num):
            exon_seq = self.target_gene.exons.seq_with_flank[i]
            exon_start = self.target_gene.exons.start[i]
            exon_end = self.target_gene.exons.end[i]
            sgrnas = []
            for pam in pams:
                pam_pattern = self._get_sgrna_pattern(pam)
                pam_pattern_rc = self._get_sgrna_pattern(
                    pam, reverse_complement=True)
                sgrnas += self._design_sgrna(exon_seq, pam_pattern, pam)
                sgrnas += self._design_sgrna(exon_seq, pam_pattern_rc,
                                             self._reverse_complement(pam),
                                             reverse_complement=True)
            for sgrna in sgrnas:
                sgrna.chrom = self.target_gene.chrom
                sgrna.gene_symbol = self.target_gene.gene_symbol
                sgrna.refseq_id = self.target_gene.refseq_id
                sgrna.exon_id = self.target_gene.exons.exon_id.values[i]
                sgrna.start += exon_start - self.flank
                sgrna.end += exon_start - self.flank

                if sgrna.rc:
                    sgrna.cutting_site = sgrna.start + 2.5
                else:
                    sgrna.cutting_site = sgrna.end - 2.5

                # print(sgrna.cutting_site)
                # print(cds_start)
                # print(cds_end)

                if (sgrna.cutting_site < cds_start) or \
                        (sgrna.cutting_site > cds_end):
                    sgrna.cutting_site_type = 'UTR'
                elif (sgrna.cutting_site >= exon_start) and \
                        (sgrna.cutting_site <= exon_end):
                    sgrna.cutting_site_type = 'coding_region'
                else:
                    sgrna.cutting_site_type = 'intron_region_near_splicing_sites'
                self.sgrnas.append(sgrna)

    def _get_sgrna_pattern(self, pam, reverse_complement=False):
        """Generate re pattern of input PAM sequence

        Args:
            pam: the PAM sequence
            reverse_complement: whether consider reverse complement of PAM

        Returns:
            re pattern to find sgRNA
        """
        if reverse_complement:
            pam = self._reverse_complement(pam)
            pam_pattern = ''
            for char in pam:
                if char == 'N':
                    pam_pattern += '\w'
                else:
                    pam_pattern += char
            sgrna_pattern = '\w{%d}%s\w{%d}\w{%d}' % (
                self.sgrna_downstream, pam_pattern,
                self.sgrna_length, self.sgrna_upstream
            )
        else:
            pam_pattern = ''
            for char in pam:
                if char == 'N':
                    pam_pattern += '\w'
                else:
                    pam_pattern += char
            sgrna_pattern = '\w{%d}\w{%d}%s\w{%d}' % (
                self.sgrna_upstream, self.sgrna_length,
                pam_pattern, self.sgrna_downstream
            )
        return sgrna_pattern

    def _design_sgrna(self, seq, pam_pattern, pam, reverse_complement=False):
        """Design sgRNAs based on re

        Args:
            seq: the sequence to be searched on
            pam_pattern: the search pattern
            pam: the PAM sequence
            reverse_complement: whether search reverse_complement of pattern

        Returns:
            a list of SgRNA object containing information for designed sgRNAs
        """
        sgrna_match = regex.finditer(pam_pattern, seq,
                                     overlapped=self.overlapped)
        sgrnas = []
        for sgrna in sgrna_match:
            full_seq = sgrna.group()
            if reverse_complement:
                if self.sgrna_upstream != 0:
                    sgrna_seq = full_seq[(self.sgrna_downstream +
                                          len(pam)):-self.sgrna_upstream]
                else:
                    sgrna_seq = full_seq[(self.sgrna_downstream + len(pam)):]
            else:
                sgrna_seq = full_seq[self.sgrna_upstream:(self.sgrna_upstream +
                                                          self.sgrna_length)]
            if self.filter_tttt:
                if reverse_complement:
                    filter_pattern = 'AAAA'
                else:
                    filter_pattern = 'TTTT'
                if sgrna_seq.find(filter_pattern) != -1:
                    continue
            if reverse_complement:
                sgrna_start = sgrna.start() + len(pam) + self.sgrna_downstream
                sgrna_end = sgrna_start + self.sgrna_length - 1
                full_seq = util.reverse_complement(full_seq)
                sgrna_strand = '-'
            else:
                sgrna_start = sgrna.start() + self.sgrna_upstream
                sgrna_end = sgrna_start + self.sgrna_length - 1
                sgrna_strand = '+'
            # if (sgrna_cutting_site < self.flank) or \
            #         (sgrna_cutting_site >= (len(seq) - self.flank)):
            #     sgrna_type = 'splicing site'
            # else:
            #     sgrna_type = 'exon region'
            sgrnas.append(SgRNA(sequence=sgrna_seq,
                                cutting_site_type=None,
                                start=sgrna_start, end=sgrna_end,
                                pam_type=pam,
                                full_seq=full_seq,
                                rc=reverse_complement,
                                strand=sgrna_strand))
        return sgrnas

    def _reverse_complement(self, sgrna_seq):
        """Get reverse complement of sequence

        Args:
            sgrna_seq: sequence

        Returns:
            str, the reverse complement of input sequence
        """
        return str(Seq(sgrna_seq).reverse_complement())

    def get_cds_info(self):
        """Compute peptide percentage of cutting site"""

        # get gene information
        gene_info = self.target_gene.gene_info
        # strand = gene_info.strand.values[0]
        # if strand == '+':
        #     cds_start = gene_info.cdsStart.values[0]
        #     cds_end = gene_info.cdsEnd.values[0]
        # else:
        #     cds_start = gene_info.cdsEnd.values[0]
        #     cds_end = gene_info.cdsStart.values[0]
        cds_start = gene_info.cdsStart.values[0]
        cds_end = gene_info.cdsEnd.values[0]
        exon_starts = np.array(
            [int(x) for x in gene_info.exonStarts.values[0].split(',') if
             x != ''])
        exon_ends = np.array(
            [int(x) for x in gene_info.exonEnds.values[0].split(',') if
             x != ''])

        # get CDS information
        replace_start_index = np.where((exon_starts <= cds_start) == True)[0][-1]
        replace_end_index = np.where((exon_ends >= cds_end) == True)[0][0]
        cds_starts = exon_starts[replace_start_index:(replace_end_index + 1)]
        cds_starts[0] = cds_start
        cds_ends = exon_ends[replace_start_index:(replace_end_index + 1)]
        cds_ends[-1] = cds_end

        return cds_starts, cds_ends

    def get_pcds(self):
        """Compute CDS percentage of cutting site"""
        cds_starts, cds_ends = self.get_cds_info()
        cds_exon_size = cds_ends - cds_starts
        cds_total_size = np.sum(cds_exon_size)

        strand = self.target_gene.gene_info.strand.values[0]

        for sgrna in self.sgrnas:
            cutting_site = sgrna.cutting_site
            for i, exon_coord in enumerate(zip(cds_starts, cds_ends)):
                start = exon_coord[0]
                end = exon_coord[1]
                if (cutting_site >= start) and (cutting_site <= end):
                    if strand == '+':
                        pcds = (cds_exon_size[:i].sum() +
                                cutting_site - start) / cds_total_size
                    else:
                        pcds = (cds_exon_size[(i + 1):].sum() +
                                end - cutting_site) / cds_total_size
                    sgrna.pcds = pcds
                    break

    def output(self):
        """Output sgRNAs in a pandas DataFrame

        Returns:
            pd.DataFrame, the cord is 0-based, both for start and end
        """
        flag = True
        self.get_pcds()
        for sgrna in self.sgrnas:
            if sgrna.rc:
                seq = sgrna.reverse_complement()
            else:
                seq = sgrna.sequence
            df_row = [sgrna.gene_symbol, sgrna.refseq_id, sgrna.exon_id,
                      sgrna.chrom, sgrna.strand,
                      sgrna.start, sgrna.end, sgrna.sequence,
                      sgrna.pam_type,
                      sgrna.cutting_site_type, sgrna.cutting_site, seq,
                      sgrna.full_seq, sgrna.pcds]
            if flag:
                df = pd.DataFrame([df_row])
                flag = False
            else:
                df = df.append([df_row])
        df.columns = ['gene_symbol', 'refseq_id', 'exon_id', 'chrom', 'strand',
                      'start', 'end', 'raw_sequence', 'pam_type',
                      'cutting_site_type', 'cutting_site', 'sgrna_seq',
                      'sgrna_full_seq', 'percent_cds']
        df.loc[:, 'cutting_site'] = df.cutting_site.astype(np.float)
        df.loc[:, 'sgrna_id'] = np.arange(0, df.shape[0])
        return df

    # def print_cutting_site(self):
    #     sgrnas_df = self.output()
    #     cutting_site_coding = sgrnas_df[
    #         sgrnas_df.cutting_site_type == 'exon region']
    #     for i in range(self.target_gene.exons.shape[0]):
    #         exon_start = self.target_gene.exons.start[i]
    #         exon_seq = self.target_gene.exons.seq[i][self.flank:-self.flank]
    #         exon_cutting_site = np.floor(
    #             cutting_site_coding[
    #                 cutting_site_coding.exon_id == str(i)].cutting_site.values)
    #         exon_cutting_site = exon_cutting_site - exon_start
    #         exon_cutting_site.sort()
    #         exon_with_cutting = ''
    #         break_start = 0
    #         for cut_index in exon_cutting_site:
    #             break_end = int(cut_index) + 1
    #             exon_with_cutting = exon_with_cutting + \
    #                                 exon_seq[break_start:break_end] + '    '
    #             break_start = break_end
    #         exon_with_cutting = exon_with_cutting + exon_seq[break_start:]
    #         print('exon id: {}'.format(i))
    #         print(exon_with_cutting)
    #         print('\n\n\n\n')

    def get_coverage_dict(self, affect_size=5):
        """The coverage of aa

        Args:
            affect_size: the up- and down- stream size from sgRNA cutting site
             which could be affected

        Returns:
            coverage_dict, a dict contains the coverage of each aa
        """

        # gene_info = self.target_gene.gene_info
        sgrna_info = self.output()
        exon_info = self.target_gene.exons
        aa_info = self.target_gene.get_aa_info()

        # aa starts from 0
        aa_dict = {}
        sgrna_dict = {}
        amino_acid_len = aa_info.shape[0]
        codon_0 = aa_info.codon_0.values
        codon_1 = aa_info.codon_1.values
        codon_2 = aa_info.codon_2.values

        sgrna_ids = sgrna_info.sgrna_id.values

        for i in range(amino_acid_len):
            aa_dict[i] = []
        for sgrna_id in sgrna_ids:
            sgrna_dict[sgrna_id] = []

        # TODO: aa_dict and sgrna_dict
        for i in range(sgrna_info.shape[0]):
            sgrna_id = sgrna_ids[i]
            exon_id = int(sgrna_info.exon_id.values[i])
            cutting_site = np.int(np.floor(sgrna_info.cutting_site.values[i]))
            exon_start = np.int(
                exon_info[exon_info.exon_id == exon_id].start.values[0])
            exon_end = np.int(
                exon_info[exon_info.exon_id == exon_id].end.values[0] - 1)

            cutting_start = max(exon_start, cutting_site - affect_size)
            cutting_end = min(exon_end, cutting_site + affect_size)
            cutting_range = np.arange(cutting_start, cutting_end + 1)

            comp_0 = np.where(np.asarray(
                [x in cutting_range for x in codon_0]) == True)[0]
            comp_1 = np.where(np.asarray(
                [x in cutting_range for x in codon_1]) == True)[0]
            comp_2 = np.where(np.asarray(
                [x in cutting_range for x in codon_2]) == True)[0]

            # coverage_sites = set(np.concatenate((comp_0, comp_1, comp_2)))
            # sgrna_info.loc[i, 'coverage_sites'] = coverage_sites

            for aa_pos in set(np.concatenate((comp_0, comp_1, comp_2))):
                sgrna_dict[sgrna_id].append(aa_pos)
                if sgrna_id not in aa_dict[aa_pos]:
                    aa_dict[aa_pos].append(sgrna_id)

        return [aa_dict, sgrna_dict]

    def select_sgrna(self, max_coverage=10, min_coverage=5):
        """Select sgRNAs for screening, for sites with more than max_coverage
        sgRNAs covered, the priorities are:
        1. NGG with high score
        2. NGG without TTTT
        3. NGG
        4. NAG without TTTT
        5. NAG

        Args:
            max_coverage: max coverage

        Returns:
            pandas DataFrame containing informatio of selected sgRNAs
        """
        aa_dict, sgrna_dict = self.get_coverage_dict()

        # Identify sgRNAs target at least one sgRNA
        sgrna_target_aa = []
        for key in sgrna_dict.keys():
            if len(sgrna_dict[key]) != 0:
                sgrna_target_aa.append(key)

        sgrna_info = self.output()
        sgrna_target_aa_info = sgrna_info[
            sgrna_info.sgrna_id.isin(sgrna_target_aa)]

        rm_sgrna_ids = []
        for key in aa_dict.keys():
            # identify aa with more than max_coverage coverage
            if len(aa_dict[key]) > max_coverage:
                # get sgRNA ids and sort by PAM types
                sgrna_ids = aa_dict[key]
                sub_info = sgrna_target_aa_info[
                    sgrna_target_aa_info.sgrna_id.isin(sgrna_ids)]
                sgrna_ids = sub_info[
                                sub_info.pam_type.isin(['NAG', 'CTN'])].append(
                    sub_info[sub_info.pam_type.isin(['NGG', 'CCN'])]).loc[:,
                            'sgrna_id'].values

                max_rm_num = len(sgrna_ids) - max_coverage
                rm_num = 0

                # test each sgRNA
                for sgrna_id in sgrna_ids:
                    cover_aa = sgrna_dict[sgrna_id]
                    flag = True
                    for aa in cover_aa:
                        if (len(aa_dict[aa]) - 1) < min_coverage:
                            flag = False
                            break
                    if flag:
                        rm_sgrna_ids.append(sgrna_id)
                        sgrna_target_aa.remove(sgrna_id)
                        # update each aa's targeting sgRNA
                        for aa in cover_aa:
                            aa_dict[aa].remove(sgrna_id)
                        rm_num += 1
                        if rm_num >= max_rm_num:
                            break

        return sgrna_target_aa_info[
            sgrna_target_aa_info.sgrna_id.isin(sgrna_target_aa)]


class Gene:
    """The gene to be edited"""

    def __init__(self, gene_symbol,
                 ref_genome='hg38',
                 uri=GENOME_EDITING_URI):
        """

        Args:
            gene_symbol: gene symbol
            table_name: the table name in db containing gene annotation
            engine: sqla engine
        """
        self.ref_genome = ref_genome
        self.gene_symbol = gene_symbol.upper()
        if ref_genome == 'mm10':
            table_name = 'ucsc_mm10_refgene'
        else:
            table_name = 'igenome_ucsc_{}_refgene'.format(self.ref_genome)
        query = "SELECT * FROM {} WHERE name2='{}'".format(table_name,
                                                           self.gene_symbol)
        self.engine = sqlalchemy.create_engine(uri)

        self.gene_info = pd.read_sql_query(query, self.engine).drop_duplicates()
        self.gene_info = self.gene_info[self.gene_info.chrom.isin(CHROMS)]
        # only retain the longest transcript
        if self.gene_info.shape[0] != 1:
            exon_nums = self.gene_info.exonCount.values
            index = np.where(exon_nums == max(exon_nums))[0][0]
            self.gene_info = pd.DataFrame(
                self.gene_info.iloc[index, :]).transpose()

        self.refseq_id = self.gene_info.name.values[0]
        self.exons = self._get_exon_info()
        self.chrom = self.gene_info.loc[:, 'chrom'].values[0]
        self.cds_start = self.gene_info.cdsStart.values
        self.cds_end = self.gene_info.cdsEnd.values
        self.cds_coord = None
        self.cds_sequence = None

    def __repr__(self):
        return self.gene_symbol

    def _get_exon_info(self):
        """Query exon information of the gene

        Returns:
            a DataFrame containing exon informations
        """
        exon_count = self.gene_info.exonCount.values[0]
        exon_starts = np.asarray(
            self.gene_info.exonStarts.values[0].split(',')[:exon_count],
            dtype=np.int)
        exon_ends = np.asarray(
            self.gene_info.exonEnds.values[0].split(',')[:exon_count],
            dtype=np.int)

        exons = pd.DataFrame(np.empty(shape=(exon_count, 5)))
        exons.columns = ['refseq_id', 'gene_symbol', 'exon_id', 'start', 'end']
        exons.loc[:, 'refseq_id'] = self.gene_info.name.values
        exons.loc[:, 'gene_symbol'] = self.gene_info.name2.values
        exons.loc[:, 'start'] = exon_starts
        exons.loc[:, 'end'] = exon_ends
        exons = exons.sort_values(by='start')
        exons.index = range(exons.shape[0])

        if self.gene_info.strand.values[0] == '+':
            exons.loc[:, 'exon_id'] = list(range(1, exon_count + 1))
        else:
            temp = list(range(1, exon_count + 1))
            temp.reverse()
            exons.loc[:, 'exon_id'] = temp

        return exons

    def get_sequence(self, flank):
        """Get exons' sequences with flank

        Args:
            flank: length of flank

        Returns:
            None, the results are stored in self.exons
        """
        self.exons.loc[:, 'seq_with_flank'] = ''
        self.exons.loc[:, 'flank'] = flank
        table_name = 'igenome_ucsc_{}_{}'.format(self.ref_genome, self.chrom)
        chrom_seq = pd.read_sql(table_name, self.engine).iloc[0, 0]
        for i in range(self.exons.shape[0]):
            # NOTE: start is 0-based but end  is 1-based
            start = self.exons.loc[:, 'start'].values[i] - flank
            end = self.exons.loc[:, 'end'].values[i] + flank
            self.exons.loc[i, 'seq_with_flank'] = \
                chrom_seq[start:end].upper()

    def get_aa_info(self):
        """Amino acid information of the gene

        Returns:
            DataFrame
        """

        exon_count = self.gene_info.exonCount.values[0]
        exon_starts = np.asarray(
            self.gene_info.exonStarts.values[0].split(',')[:exon_count],
            dtype=np.int)
        exon_ends = np.asarray(
            self.gene_info.exonEnds.values[0].split(',')[:exon_count],
            dtype=np.int)
        cds_start = self.gene_info.cdsStart.values[0]
        cds_end = self.gene_info.cdsEnd.values[0]
        cds_start_exon_index = list(
            (cds_start >= exon_starts) & (cds_start <= exon_ends)).index(True)
        cds_end_exon_index = list(
            (cds_end >= exon_starts) & (cds_end <= exon_ends)).index(True)
        cds_starts = exon_starts[
                     cds_start_exon_index:(cds_end_exon_index + 1)].copy()
        cds_ends = exon_ends[
                   cds_start_exon_index:(cds_end_exon_index + 1)].copy()
        cds_starts[0] = cds_start
        cds_ends[-1] = cds_end
        table_name = 'igenome_ucsc_{}_{}'.format(self.ref_genome, self.chrom)
        chrom_seq = pd.read_sql(table_name, self.engine).iloc[0, 0]
        seq = ''
        coord = []
        for i in range(len(cds_starts)):
            start = cds_starts[i]
            end = cds_ends[i]
            seq += chrom_seq[start:end].upper()
            coord += range(start, end)

        if self.gene_info.strand.values[0] == '+':
            coding_dna = Seq(seq, IUPAC.unambiguous_dna)
        else:
            coding_dna = Seq(seq, IUPAC.unambiguous_dna).reverse_complement()
            coord.reverse()
        protein_seq = str(coding_dna.translate())

        aa_info = pd.DataFrame(np.zeros(shape=[len(protein_seq), 5]))
        aa_info.columns = ['amino_acid', 'codon_0', 'codon_1', 'codon_2',
                           'aa_index']
        pointer = 0
        for i in range(len(protein_seq)):
            aa_info.loc[i, 'amino_acid'] = protein_seq[i]
            aa_coord = coord[pointer:(pointer + 3)]
            aa_info.loc[i, 'codon_0'] = int(aa_coord[0])
            aa_info.loc[i, 'codon_1'] = int(aa_coord[1])
            aa_info.loc[i, 'codon_2'] = int(aa_coord[2])
            pointer += 3
        aa_info.loc[:, 'aa_index'] = list(range(aa_info.shape[0]))

        return aa_info

    def get_cds_exon(self, tx_starts, tx_ends, cds_start, cds_end):
        cds_index = np.where(((tx_starts >= cds_start) &
                              (tx_ends <= cds_end)) == True)[0]
        cds_index_start = cds_index[0] - 1
        cds_index_end = cds_index[-1] + 1

        cds_starts = tx_starts[cds_index_start: (cds_index_end + 1)]
        cds_ends = tx_ends[cds_index_start: (cds_index_end + 1)]
        cds_starts[0] = cds_start
        cds_ends[-1] = cds_end
        cds_coord = list(zip(cds_starts, cds_ends))
        return cds_coord

    # TODO: BUG FIX
    def get_cds(self, upstream=100, downstream=100):
        """Get the CDS of the gene. For alternative splicing, get all possible
        sequences.

        Args:
            upstream: the length of sequence upstream of CDS
            downstream: the length of sequence downstream of CDS

        Returns:
            upstream, CDS and downstream sequences
        """
        table_name = 'igenome_ucsc_{}_refgene'.format(self.ref_genome)
        query = "SELECT * FROM {} WHERE name2='{}'".format(table_name,
                                                           self.gene_symbol)
        gene_info = pd.read_sql_query(query, self.engine).drop_duplicates()

        # cds_start = gene_info.cdsStart.min()
        # cds_end = gene_info.cdsEnd.max()

        # get all exon coords
        all_exons = []
        for i in range(gene_info.shape[0]):
            transcript_info = gene_info.iloc[i, :]
            exon_starts = [int(x) for x in
                           transcript_info.exonStarts.split(',')[:-1]]
            exon_ends = [int(x) for x in
                         transcript_info.exonEnds.split(',')[:-1]]
            cds_start = transcript_info.cdsStart
            cds_end = transcript_info.cdsEnd
            transcript_coord = self.get_cds_exon(exon_starts, exon_ends,
                                                 cds_start, cds_end)
            all_exons = list(set(all_exons + transcript_coord))
        all_exons.sort()

        # get cds coord
        cds_coord = []
        for i, exon_coord in enumerate(all_exons):
            if i == 0:
                cds_coord.append(exon_coord)
            else:
                prev_exon = cds_coord[-1]
                prev_end = prev_exon[1]
                if exon_coord[0] <= prev_end:
                    if exon_coord[1] <= prev_end:
                        continue
                    else:
                        cds_coord[-1] = (prev_exon[0], exon_coord[1])
                else:
                    cds_coord.append(exon_coord)

        # # replace txStart and txEnd with cds_start and cds_end
        # tx_starts = np.array([x[0] for x in tx_coord])
        # tx_ends = np.array([x[1] for x in tx_coord])
        #
        # cds_index = np.where(((tx_starts >= cds_start) &
        #                       (tx_ends <= cds_end)) == True)[0]
        # cds_index_start = cds_index[0] - 1
        # cds_index_end = cds_index[-1] + 1
        #
        # cds_starts = tx_starts[cds_index_start: (cds_index_end + 1)]
        # cds_ends = tx_ends[cds_index_start: (cds_index_end + 1)]
        # cds_starts[0] = cds_start
        # cds_ends[-1] = cds_end
        # cds_coord = list(zip(cds_starts, cds_ends))
        self.cds_coord = cds_coord

        table_name = 'igenome_ucsc_{}_{}'.format(self.ref_genome, self.chrom)
        chrom_seq = pd.read_sql(table_name, self.engine).iloc[0, 0]
        cds_seq = ''
        for exon_coord in self.cds_coord:
            # NOTE: start is 0-based but end  is 1-based
            start = exon_coord[0]
            end = exon_coord[1]
            cds_seq += chrom_seq[start:end].upper()
        self.cds_sequence = cds_seq

        cds_start_overall = gene_info.cdsStart.min()
        cds_end_overall = gene_info.cdsEnd.max()

        upstream_seq = chrom_seq[(cds_start_overall -
                                  upstream):cds_start_overall].upper()
        downstream_seq = chrom_seq[cds_end_overall:(cds_end_overall
                                                    + downstream)].upper()

        return upstream_seq, cds_seq, downstream_seq


class SgRNA:
    """SgRNA targeting a region of the genome"""

    def __init__(self, sequence=None, pam_type=None, cutting_site_type=None,
                 gene_symbol=None, chrom=None, start=None, end=None,
                 exon_id=None, cutting_site=None, full_seq=None,
                 aa_cut=None, per_peptide=None, rs2_score=None,
                 rc=None, refseq_id=None, strand=None, pcds=None):
        """

        Args:
            sequence: sgRNA sequence
            pam_type: PAM sequence
            cutting_site_type: coding region or splicing site
            gene_symbol: gene symbol
            chrom: chromosome
            start: start position, 0 based
            end: end position, 0 based
            exon_id: exon ID
            cutting_site: the position of cutting site
            full_seq: full sequence of sgRNA including up- and down- stream
            aa_cut: cut position
            per_peptide: cut peptide position
            rs2_score: rs2 score
            rc:
            refseq_id: refseq ID
        """

        self.sequence = sequence
        self.pam_type = pam_type
        self.gene_symbol = gene_symbol
        self.chrom = chrom
        self.start = start
        self.end = end
        self.cutting_site = cutting_site
        self.cutting_site_type = cutting_site_type
        self.exon_id = exon_id
        self.full_seq = full_seq
        self.aa_cut = aa_cut
        self.per_peptide = per_peptide
        self.rc = rc
        self.refseq_id = refseq_id
        self.strand = strand
        self.pcds = pcds
        # compute rs2 score
        # self.rs2_score = rs2_score
        # if rs2_score is not None:
        #     self.rs2_score = rs2_score
        # else:
        #     self.rs2_score = compute_rs2(self.sequence, self.aa_cut,
        #                                  self.per_peptide)

    def __repr__(self):
        return self.sequence

    def get_gc_content(self):
        """GC content of the sgRNA

        Returns:
            GC content
        """
        if self.sequence is None:
            return None
        else:
            gc_content = (self.sequence.count('G') +
                          self.sequence.count('C')) / len(self.sequence)
            return gc_content

    def reverse_complement(self):
        """Get reverse complement of sequence

        Returns:
            str, the reverse complement of input sequence
        """
        return str(Seq(self.sequence).reverse_complement())

    # def print(self):
    #     """Print the SgRNA object
    #
    #     Returns:
    #         None
    #     """
    #     print('entrez_id: {}, exon_id: {}, chrom: {}, start: {}, end: {}, '
    #           'sequence: {}, PAM_type: {}, cutting_site_type: {}, '
    #           'cutting_site: {}'.format(
    #         self.entrez_id, self.exon_id, self.chrom, self.start, self.end,
    #         self.sequence, self.pam_type, self.cutting_site_type,
    #         self.cutting_site
    #     ))

    def get_rs2_score(self):
        """Compute rs2 score of the sgRNA

        Returns:
            rs2 score
        """
        assert self.pam_type == 'NGG', 'Only support NGG'
        assert len(self.full_seq) == 30, 'Have to provide 30mers'
        return compute_rs2(self.full_seq, self.aa_cut, self.per_peptide)

    def get_offtarget_info(self):
        """Get multiple target information of the sgRNA

        Returns:
            DataFrame
        """
        return alignment.bowtie_alignment(self.sequence + self.pam_type,
                                          report_all=True)


class Transcript(Gene):
    def __init__(self, refseq_id,
                 ref_genome='hg38',
                 uri=GENOME_EDITING_URI):
        """Init

        Args:
            entrez_id: the ENTREZ ID of the gene
            table_name: the table in the database storing exon information
            engine: sqlalchemy engine
        """
        self.refseq_id = refseq_id.upper()
        self.ref_genome = ref_genome
        table_name = 'igenome_ucsc_{}_refgene'.format(self.ref_genome)
        query = "SELECT * FROM {} WHERE name='{}'".format(table_name,
                                                          self.refseq_id)
        self.engine = sqlalchemy.create_engine(uri)
        self.gene_info = pd.read_sql_query(query, self.engine).drop_duplicates()

        # map到多个位置，保留经典染色体上的信息，如果仍有重复，则根据第一个计算
        if self.gene_info.shape[0] > 1:
            self.gene_info = self.gene_info[self.gene_info.chrom.isin(CHROMS)]
            self.gene_info = self.gene_info.iloc[0:1, :]

        self.gene_symbol = self.gene_info.name2.values[0]
        self.exons = self._get_exon_info()
        self.chrom = self.gene_info.loc[:, 'chrom'].values[0]
        self.cds_start = self.gene_info.cdsStart.values
        self.cds_end = self.gene_info.cdsEnd.values

    def __repr__(self):
        return self.refseq_id


class SeqDesigner(Designer):
    def __init__(self, seq, sgrna_upstream=4, sgrna_downstream=3,
                 sgrna_length=20, flank=30, overlapped=True, filter_tttt=False):
        """

        Args:
            seq: sequence to be designed
        """

        super(SeqDesigner, self).__init__(sgrna_upstream=sgrna_upstream,
                                          sgrna_downstream=sgrna_downstream,
                                          sgrna_length=sgrna_length,
                                          flank=flank,
                                          overlapped=overlapped,
                                          filter_tttt=filter_tttt)
        self.seq = seq.upper()

    def __repr__(self):
        return 'SeqDesigner'

    def get_sgrnas(self, pams=['NGG', 'NAG']):
        """Get sgRNAs with PAM NGG and NAG

        Args:
            pams: the PAM to design

        Returns:
            None. The results are stored in self.sgrnas
        """
        sgrnas = []
        for pam in pams:
            pam_pattern = self._get_sgrna_pattern(pam)
            pam_pattern_rc = self._get_sgrna_pattern(
                pam, reverse_complement=True)
            sgrnas += self._design_sgrna(self.seq, pam_pattern, pam)
            sgrnas += self._design_sgrna(self.seq, pam_pattern_rc,
                                         self._reverse_complement(pam),
                                         reverse_complement=True)
        for sgrna in sgrnas:
            if sgrna.rc:
                sgrna.cutting_site = sgrna.start + 2.5
            else:
                sgrna.cutting_site = sgrna.end - 2.5
            self.sgrnas.append(sgrna)

    def output(self):
        """Output sgRNAs in a pandas DataFrame

        Returns:
            pd.DataFrame, the cord is 0-based, both for start and end
        """
        flag = True
        for sgrna in self.sgrnas:
            if sgrna.rc:
                seq = sgrna.reverse_complement()
            else:
                seq = sgrna.sequence
            df_row = [sgrna.start, sgrna.end, sgrna.sequence, sgrna.pam_type,
                      sgrna.cutting_site, seq, sgrna.full_seq]
            if flag:
                df = np.asarray(df_row)
                flag = False
            else:
                df = np.vstack((df, df_row))
        df = pd.DataFrame(df)
        df.columns = ['start', 'end', 'raw_sequence', 'pam_type',
                      'cutting_site', 'sgrna_seq', 'sgrna_full_seq']
        df.loc[:, 'cutting_site'] = df.cutting_site.astype(np.float)
        df.loc[:, 'sgrna_id'] = np.arange(0, df.shape[0])
        return df


def build_screen_library(inputs, sgrna_num=3, ref_genome='hg38',
                         mode='gene_symbol', off_target_tol='standard',
                         pam='NGG'):
    """Build screen library for a gene list. In gene_symbol mode, for gene that
    have multiple transcripts, we will design sgRNAs for each transcript.

    Args:
        inputs:
        sgrna_num:
        ref_genome:
        pam:
        mode: ('gene_symbol', 'refseq_id')

    Returns:

    """
    engine = sqlalchemy.create_engine(GENOME_EDITING_URI)

    # check input
    assert mode in ('gene_symbol', 'refseq_id'), 'Wrong mode'
    assert off_target_tol in ('high', 'standard', 'low', False), \
        'Wrong rm_off_target'
    assert pam == 'NGG', 'Wrong PAM'

    # get the refseq IDs of the input
    if mode == 'gene_symbol':
        table_name = 'igenome_ucsc_{}_refgene'.format(ref_genome)
        gene_info = pd.read_sql(table_name, engine)
        refseq_ids = gene_info[gene_info.name2.isin(inputs)].name.values
    else:
        refseq_ids = inputs

    # get the tolerance of off-targets
    if off_targets == 'high':
        seed_len = 20
    elif off_targets == 'standard':
        seed_len = 16
    elif off_targets == 'low':
        seed_len = 12
    else:
        seed_len = False

    flag = True
    for refseq_id in refseq_ids:
        # design all possible sgRNAs
        # TODO: peptide percent and GC content
        sgrna_designer = Designer(refseq_id=refseq_id,
                                  sgrna_upstream=4,
                                  ref_genome=ref_genome,
                                  sgrna_downstream=3, sgrna_length=20,
                                  flank=30, filter_tttt=False)
        sgrna_designer.get_sgrnas(pam)
        design_output = sgrna_designer.output()

        # remove sgRNAs don't target coding region
        design_output = design_output[
            design_output.cutting_site_type.isin(['coding_region'])]

        # remove sgRNAs with off-targets
        if seed_len:
            sgrna_seqs = design_output.sgrna_seq.values
            have_off_target = []
            for seq in sgrna_seqs:
                have_off_target.append(
                    off_targets.have_off_targets(seq, pam,
                                                 upstream_len=seed_len,
                                                 num_mismatch=0))
            design_output = design_output.loc[~have_off_target, :]

        # score
        score_seqs = design_output.sgrna_full_seq.values
        deep_rank_score = []
        # TODO: function: input: seq and feats, output: score

        # Pick sgRNA
        sub_design_output = pick_top_sgrna(design_output, sgrna_num)
        if flag:
            screen_library = sub_design_output
            flag = False
        else:
            screen_library = screen_library.append(sub_design_output)
    return screen_library


def pick_top_sgrna(design_output, sgrna_num):
    return design_output.iloc[:sgrna_num]


def design_sgrna_with_offtargets(refseq_id, ref_genome='hg19', pam='NGG',
                                 bowtie_index=os.getenv(
                                     'HG19_BOWTIE_INDEX_PATH')):
    design = Designer(refseq_id=refseq_id, ref_genome=ref_genome)
    design.get_sgrnas(pams=[pam])
    design_out = design.output()
    sgrna_seqs = design_out.sgrna_seq.values
    offtarget_12mer = off_targets.sgrna_off_targets_batch(
        sgrna_seqs, pam, seed=12, bowtie_index=bowtie_index)
    offtarget_16mer = off_targets.sgrna_off_targets_batch(
        sgrna_seqs, pam, seed=16, bowtie_index=bowtie_index)
    offtarget_20mer = off_targets.sgrna_off_targets_batch(
        sgrna_seqs, pam, seed=20, bowtie_index=bowtie_index)
    design_out.loc[:, 'offtarget_12mer'] = offtarget_12mer
    design_out.loc[:, 'offtarget_16mer'] = offtarget_16mer
    design_out.loc[:, 'offtarget_20mer'] = offtarget_20mer
    return design_out
