import click

from lifetracker.cli.extract_eml import extract_eml_command
from lifetracker.cli.extract_fidelity_pdfs import extract_fidelity_pdfs_command
from lifetracker.cli.filter_zip import filter_zip_command
from lifetracker.cli.process_dates import process_dates_command
from lifetracker.cli.zip_eml_to_pdf import zip_eml_to_pdf_command
from lifetracker.cli.zip_eml_to_txt import zip_eml_to_txt_command


@click.group()
def cli():
    """lifetracker command-line utilities."""


cli.add_command(extract_eml_command)
cli.add_command(extract_fidelity_pdfs_command)
cli.add_command(filter_zip_command)
cli.add_command(process_dates_command)
cli.add_command(zip_eml_to_pdf_command)
cli.add_command(zip_eml_to_txt_command)
