import re
import logging
import click
import yaml
from tqdm import tqdm
from pathlib import Path

from login import USERNAME, PASSWORD
from helper import (
    get_subcategories,
    fetch_wikidata_item,
    get_files_in_category,
)
from wdcuration import query_wikidata
from wikibaseintegrator import wbi_login, WikibaseIntegrator, wbi_enums
from wikibaseintegrator.datatypes import CommonsMedia, Item
from wikibaseintegrator.wbi_config import config as wbi_config
from wikibaseintegrator.models import References, Reference
# Configure WikibaseIntegrator for Wikidata
wbi_config['MEDIAWIKI_API_URL'] = 'https://www.wikidata.org/w/api.php'
wbi_config['SPARQL_ENDPOINT_URL'] = 'https://query.wikidata.org/sparql'
wbi_config['WIKIBASE_URL'] = 'https://www.wikidata.org'
wbi_config['USER_AGENT'] = 'TiagoLubianaBot (https://meta.wikimedia.org/wiki/User:TiagoLubianaBot)'

# Login as TiagoLubianaBot
login_instance = wbi_login.Login(
    user=USERNAME,
    password=PASSWORD,
    mediawiki_api_url=wbi_config['MEDIAWIKI_API_URL']
)
wbi = WikibaseIntegrator(login=login_instance)

# Path to YAML file for categories with 3+ images
YAML_PATH = Path("categories_to_review.yaml")

@click.command()
@click.argument('category')
@click.option('--verbose', is_flag=True, help='Enable verbose output for debugging.')
def process_category(category, verbose):
    """
    CLI tool to process Wikimedia Commons categories and update Wikidata
    based on the number of images found in each category.
    """
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING)
    logging.info(f"Processing category: {category}")

    categories_to_review = {}

    # 1) Fetch subcategories recursively
    genera = get_subcategories(category, verbose=verbose)
    logging.info(f"Top-level category has {len(genera)} subcategories.")

    # 2) Process each subcategory (genus) → get sub-subcategories (taxa)
    for genus in tqdm(genera, desc="Processing genera"):
        if "Unidentified" in genus:
            continue

        taxa = get_subcategories(genus, verbose=verbose)

        for taxon in tqdm(taxa, desc=f"Processing taxa in {genus}", leave=False):
            match = re.match(r"([^\\-]+) - botanical illustrations", taxon)
            if not match:
                logging.info(f"Skipping taxon {taxon}: no match for regex.")
                continue

            species_name = match.group(1)
            wikidata_item = fetch_wikidata_item(species_name, verbose=verbose)

            if not wikidata_item:
                continue

            # 3) Get all files in the taxon category
            files = get_files_in_category(taxon, verbose=verbose)
            file_count = len(files)

            # 4) Apply your logic based on the number of images
            if file_count == 1 or file_count == 2:
                process_files(wikidata_item, files)
            elif file_count >= 3:
                categories_to_review[taxon] = files

    # 5) Save categories needing manual review
    if categories_to_review:
        save_to_yaml(categories_to_review)

    print("Processing complete. Wikidata updated.")

def process_files(wikidata_item: str, files: list):
    """
    Determine which properties (P18 or P13162) should be added to a
    Wikidata item and do so in one write, if needed.
    """
    # 1) Check existing claims for P18/P13162
    existing_p18 = get_existing_claims(wikidata_item, "P18")
    existing_p13162 = get_existing_claims(wikidata_item, "P13162")

    # 2) Decide what to add (none, P18, or P13162)
    add_prop = None
    if not existing_p18 and not existing_p13162:
        add_prop = "P18"
    elif existing_p18 and not existing_p13162:
        add_prop = "P13162"

    if not add_prop:
        # Means we have both P18 and P13162 => skip
        logging.info(f"Skipping {wikidata_item}, already has P18 and P13162.")
        return

    # 3) Fetch item data once, add claims, then write
    item = wbi.item.get(entity_id=wikidata_item)
    references = create_reference()

    for file_name in files:
        item.claims.add(
            CommonsMedia(prop_nr=add_prop, value=file_name, references=references),
            action_if_exists=wbi_enums.ActionIfExists.MERGE_REFS_OR_APPEND
        )

    summary = f"Adding {add_prop} claims via TiagoLubianaBot"
    try:
        logging.info(f"Updating {wikidata_item}: Adding {add_prop} for {files}")
        item.write(summary=summary)
    except Exception as e:
        logging.error(f"Failed to update {wikidata_item}: {e}")

def get_existing_claims(wikidata_item: str, prop_nr: str) -> list:
    """
    Fetch existing claims for a Wikidata item and return them as a list.
    (We only need to know if ANY claims exist, but let's store them for clarity)
    """
    query = f"""
    SELECT ?value WHERE {{
      wd:{wikidata_item} wdt:{prop_nr} ?value.
    }}
    """
    results = query_wikidata(query)
    if not results:
        return []
    return [r["value"].split("/")[-1] for r in results]

def create_reference():
    """
    Creates a reference object, 'Inferred from Wikimedia Commons' (P887=Q131478853).
    """
    references = References()
    ref_obj = Reference()
    ref_obj.add(Item(prop_nr="P887", value="Q131478853"))  # Inferred from Wikimedia Commons
    references.add(ref_obj)
    return references

def save_to_yaml(data: dict):
    """
    Saves categories with 3+ images to a YAML file so they can be handled manually.
    """
    if YAML_PATH.exists():
        with YAML_PATH.open("r", encoding="utf-8") as f:
            existing_data = yaml.safe_load(f) or {}
    else:
        existing_data = {}

    existing_data.update(data)

    with YAML_PATH.open("w", encoding="utf-8") as f:
        yaml.dump(existing_data, f, default_flow_style=False, allow_unicode=True)

    logging.info(f"Saved categories with 3+ images to {YAML_PATH}")

if __name__ == '__main__':
    process_category()
