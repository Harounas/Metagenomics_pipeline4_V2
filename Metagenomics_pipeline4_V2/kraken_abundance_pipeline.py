#!/usr/bin/env python3
"""
kraken_abundance_pipeline.py (Version 2)

This module processes Kraken2 reports, generates abundance plots, aggregates results (with metadata or sample IDs), and supports quality control via MultiQC. It supports de novo assembly via MetaSPAdes, optional host depletion via Bowtie2, and allows skipping preprocessing steps.
"""

import os
import glob
import pandas as pd
import logging
import subprocess
import random
from collections import defaultdict
import plotly.express as px

# Local imports
from .trimmomatic import run_trimmomatic
from .metaspades import run_spades
from .bowtie2 import run_bowtie2
from .kraken2 import run_kraken2

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

def process_sample(forward, reverse, base_name, bowtie2_index, kraken_db, output_dir, threads,
                   run_bowtie, use_precomputed_reports, use_assembly,
                   skip_preprocessing=False, skip_existing=False):
    try:
        kraken_report = os.path.join(output_dir, f"{base_name}_kraken_report.txt")
        output_report = os.path.join(output_dir, f"{base_name}_kraken2_output.txt")

        if use_precomputed_reports:
            if not os.path.exists(kraken_report) or not os.path.exists(output_report):
                raise FileNotFoundError(f"Precomputed Kraken2 report or output report not found for {base_name}")
            return kraken_report, output_report

        if skip_preprocessing:
            contigs_file = os.path.join(output_dir, f"{base_name}_contigs.fasta")
            if skip_existing and os.path.exists(contigs_file):
                logging.info(f"[SKIP] Contigs exist for {base_name}, skipping assembly.")
            else:
                logging.info(f"Running SPAdes for sample {base_name}")
                contigs_file = run_spades(forward, reverse, base_name, output_dir, threads)
            kraken_input = contigs_file
        else:
            trimmed_forward = os.path.join(output_dir, f"{base_name}_1_trimmed_paired.fq.gz")
            trimmed_reverse = os.path.join(output_dir, f"{base_name}_2_trimmed_paired.fq.gz")

            if not skip_existing or not (os.path.exists(trimmed_forward) and os.path.exists(trimmed_reverse)):
                logging.info(f"Running Trimmomatic for sample {base_name}")
                run_trimmomatic(forward, reverse, base_name, output_dir, threads)

            unmapped_r1, unmapped_r2 = trimmed_forward, trimmed_reverse
            if run_bowtie:
                bowtie_unmapped_r1 = os.path.join(output_dir, f"{base_name}_1_unmapped.fq.gz")
                bowtie_unmapped_r2 = os.path.join(output_dir, f"{base_name}_2_unmapped.fq.gz")
                if not skip_existing or not (os.path.exists(bowtie_unmapped_r1) and os.path.exists(bowtie_unmapped_r2)):
                    logging.info(f"Running Bowtie2 for sample {base_name}")
                    run_bowtie2(trimmed_forward, trimmed_reverse, base_name, bowtie2_index, output_dir, threads)
                unmapped_r1, unmapped_r2 = bowtie_unmapped_r1, bowtie_unmapped_r2

            kraken_input = run_spades(unmapped_r1, unmapped_r2, base_name, output_dir, threads) if use_assembly else unmapped_r1

        if not skip_existing or not os.path.exists(kraken_report):
            logging.info(f"Running Kraken2 for sample {base_name}")
            if use_assembly or skip_preprocessing:
                run_kraken2(kraken_input, None, base_name, kraken_db, output_dir, threads)
            else:
                run_kraken2(unmapped_r1, unmapped_r2, base_name, kraken_db, output_dir, threads)

        # Process output report as well
        if not skip_existing or not os.path.exists(output_report):
            logging.info(f"Running Output Analysis for sample {base_name}")
            process_output_report(output_report, output_dir)

        return kraken_report, output_report

    except Exception as e:
        logging.error(f"Error processing sample {base_name}: {e}")
        return None, None

def process_output_report(output_report, output_dir):
    """
    Processes output report files by splitting them into domain-specific files.
    Output files are named as: {sample}_{DomainWithoutSpaces}_output_report.txt.
    """
    domain_labels = {'Viruses', 'Eukaryota', 'Bacteria', 'Archaea'}
    try:
        with open(output_report, 'r') as file:
            lines = file.readlines()

        current_domain = None
        current_rows = []
        for line in lines:
            columns = line.strip().split("\t")
            if len(columns) < 6:
                continue
            rank_code = columns[3]

            if rank_code == "D":  # Domain level
                if current_domain:
                    save_domain_data(current_domain, current_rows, output_dir)
                current_domain = columns[5]  # Domain name (e.g., Viruses, Eukaryota)
                current_rows = [line]
            else:
                current_rows.append(line)

        # Save the last domain data
        if current_domain:
            save_domain_data(current_domain, current_rows, output_dir)

    except Exception as e:
        logging.error(f"Error processing output report {output_report}: {e}")


def save_domain_data(domain, rows, output_dir):
    """
    Saves domain-specific data into a file.
    """
    domain_file_name = f"{domain.replace(' ', '')}_kraken2_output.txt"
    domain_file_path = os.path.join(output_dir, domain_file_name)

    with open(domain_file_path, 'w') as f:
        for row in rows:
            f.write(row)

    logging.info(f"Saved {domain} data to {domain_file_path}")

def generate_sample_ids_csv(kraken_dir):
    """
    Generates a CSV containing sample IDs extracted from Kraken report filenames.

    Parameters:
      kraken_dir (str): Directory with Kraken report files.

    Returns:
      str: Path to the generated sample_ids.csv file.
    """
    try:
        sample_ids = [fname.replace('_kraken_report.txt', '')
                      for fname in os.listdir(kraken_dir)
                      if fname.endswith('_kraken_report.txt')]
        sample_ids_df = pd.DataFrame({'Sample_ID': sample_ids})
        csv_path = os.path.join(kraken_dir, 'sample_ids.csv')
        sample_ids_df.to_csv(csv_path, index=False)
        logging.info(f"Sample IDs written to {csv_path}")
        return csv_path
    except Exception as e:
        logging.error(f"Error generating sample IDs CSV: {e}")
        return None


def process_kraken_reports(kraken_dir):
    """
    Processes Kraken2 report files by splitting them into domain-specific files.
    Output files are named as: {sample}_{DomainWithoutSpaces}_kraken_report.txt.
    
    Parameters:
      kraken_dir (str): Directory with Kraken report files.
    """
    domain_labels = {'Viruses', 'Eukaryota', 'Bacteria', 'Archaea'}
    for file_name in os.listdir(kraken_dir):
        if file_name.endswith("_report.txt"):
            kraken_report_path = os.path.join(kraken_dir, file_name)
            sample_name = clean_sample_name(file_name, domain_labels)
            domains = extract_domains_from_kraken_report(kraken_report_path)
            for domain, df in domains.items():
                output_filename = f"{sample_name}_{domain.replace(' ', '')}_kraken_report.txt"
                output_path = os.path.join(kraken_dir, output_filename)
                df.to_csv(output_path, sep="\t", index=False, header=False)
                logging.info(f"Saved {domain} data to {output_path}")

def process_output_reports(kraken_dir):
    """
    Processes output.txt files by splitting them into domain-specific files.
    Output files are named as: {sample}_{DomainWithoutSpaces}_output_report.txt.
    """
    domain_labels = {'Viruses', 'Eukaryota', 'Bacteria', 'Archaea'}
    for file_name in os.listdir(kraken_dir):
        if file_name.endswith("_kraken2_output.txt"):
            output_report_path = os.path.join(kraken_dir, file_name)
            sample_name = clean_sample_name(file_name, domain_labels)
            process_output_report(output_report_path, kraken_dir)

# Helper function to clean sample name
def clean_sample_name(file_name, domain_labels):
    """
    Removes domain labels and the _report.txt suffix from a filename to obtain a clean sample name.
    """
    name = file_name.replace("_report.txt", "")
    parts = name.split("_")
    cleaned_parts = [p for p in parts if p not in domain_labels]
    return "_".join(cleaned_parts)

def extract_domains_from_kraken_report(kraken_report_path):
    """
    Extracts rows for each domain (Bacteria, Eukaryota, Archaea, Viruses) from a Kraken2 report.
    Returns a dictionary with keys as domain names and values as DataFrames.
    """
    columns = ["Percentage", "Reads_Covered", "Reads_Assigned", "Rank_Code", "NCBI_TaxID", "Scientific_Name"]
    df = pd.read_csv(kraken_report_path, sep="\t", header=None, names=columns)
    domains = {}
    current_domain = None
    current_rows = []
    for _, row in df.iterrows():
        if row["Rank_Code"] == "D":
            if current_domain:
                domains[current_domain] = pd.DataFrame(current_rows)
            current_domain = row["Scientific_Name"]
            current_rows = [row]
        else:
            current_rows.append(row)
    if current_domain:
        domains[current_domain] = pd.DataFrame(current_rows)
    return domains

def aggregate_kraken_results(kraken_dir, metadata_file=None, sample_id_df=None,
                             read_count=1, max_read_count=10**30, rank_code='S', domain_filter=None):
    """
    Aggregates Kraken results at a specified rank code, applying per-domain read count filtering.
    
    For example, at species level (default 'S'), rows with Rank_code in ['S', 'S1', 'S2', 'S3']
    are selected; at Family level ('F'), rows with Rank_code in ['F', 'F1', 'F2', 'F3'] are selected,
    and so on.
    
    Args:
        - min_read_counts (dict): Dictionary of minimum read count per domain.
        - max_read_counts (dict): Dictionary of maximum read count per domain.
    """
    try:
        # Load metadata
        if metadata_file:
            metadata = pd.read_csv(metadata_file, sep=",")
            logging.info("Using metadata from the provided file.")
        elif sample_id_df is not None:
            metadata = sample_id_df
            logging.info("Using sample IDs as metadata.")
        else:
            raise ValueError("Either metadata_file or sample_id_df must be provided.")

        sample_id_col = metadata.columns[0]
        aggregated_results = {}

        # Define rank mapping: keys are desired rank level; values are acceptable rank codes.
        rank_mapping = {
            'S': ['S', 'S1', 'S2', 'S3'],
            'K': ['K', 'K1', 'K2', 'K3'],
            'F': ['F', 'F1', 'F2', 'F3'],
            'D': ['D', 'D1', 'D2', 'D3']
        }

        for file_name in os.listdir(kraken_dir):
            if file_name.endswith("_report.txt"):
                with open(os.path.join(kraken_dir, file_name), 'r') as f:
                    for line in f:
                        fields = line.strip().split('\t')
                        if len(fields) < 6:
                            continue
                        perc_frag_cover = fields[0]
                        nr_frag_cover = fields[1]
                        nr_frag_direct_at_taxon = int(fields[2])
                        rank_code_field = fields[3]
                        ncbi_ID = fields[4]
                        scientific_name = fields[5]
                        parts = file_name.split('_')
                        extracted_part = '_'.join(parts[:-2])
                        sampleandtaxonid = f"{extracted_part}{ncbi_ID}"

                        if rank_code_field in rank_mapping.get(rank_code, [rank_code]) and \
                           (read_count <= nr_frag_direct_at_taxon <= max_read_count):
                            if extracted_part in metadata[sample_id_col].unique():
                                sample_metadata = metadata.loc[metadata[sample_id_col] == extracted_part].iloc[0].to_dict()
                                aggregated_results[sampleandtaxonid] = {
                                    'Perc_frag_cover': perc_frag_cover,
                                    'Nr_frag_cover': nr_frag_cover,
                                    'Nr_frag_direct_at_taxon': nr_frag_direct_at_taxon,
                                    'Rank_code': rank_code_field,
                                    'NCBI_ID': ncbi_ID,
                                    'Scientific_name': scientific_name,
                                    'SampleID': extracted_part,
                                    **sample_metadata
                                }

        merged_tsv_path = os.path.join(kraken_dir, f"merged_kraken_{rank_code}.tsv")
        with open(merged_tsv_path, 'w') as f:
            headers = ['Perc_frag_cover', 'Nr_frag_cover', 'Nr_frag_direct_at_taxon',
                       'Rank_code', 'NCBI_ID', 'Scientific_name', 'SampleID'] + metadata.columns[1:].tolist()
            f.write("\t".join(headers) + "\n")
            for data in aggregated_results.values():
                f.write("\t".join(str(data[col]) for col in headers) + "\n")

        logging.info(f"Aggregated results saved to {merged_tsv_path}")
        return merged_tsv_path

    except Exception as e:
        logging.error(f"Error aggregating Kraken results: {e}")
        return None
