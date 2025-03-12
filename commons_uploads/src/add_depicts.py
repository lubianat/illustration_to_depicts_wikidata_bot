import re
import logging
import click
import yaml
from tqdm import tqdm
from pathlib import Path
import urllib.parse
import random

from login import USERNAME, PASSWORD
from helper import (
    get_subcategories,
    get_qid_from_taxon_name,
    get_files_in_category,
    get_existing_claims,
    build_commons_file_permalink,
)
from wikibaseintegrator import wbi_login, WikibaseIntegrator, wbi_enums
from wikibaseintegrator.datatypes import CommonsMedia, Item, URL
from wikibaseintegrator.wbi_config import config as wbi_config
from wikibaseintegrator.models import References, Reference
import requests

# Configure WikibaseIntegrator for Wikidata
wbi_config["MEDIAWIKI_API_URL"] = "https://commons.wikimedia.org/w/api.php"
wbi_config["SPARQL_ENDPOINT_URL"] = "https://query.wikidata.org/sparql"
wbi_config["WIKIBASE_URL"] = "https://commons.wikimedia.org"
wbi_config["USER_AGENT"] = (
    "TiagoLubianaBot (https://meta.wikimedia.org/wiki/User:TiagoLubianaBot)"
)

# Login as TiagoLubianaBot
login_instance = wbi_login.Login(
    user=USERNAME, password=PASSWORD, mediawiki_api_url=wbi_config["MEDIAWIKI_API_URL"]
)
wbi = WikibaseIntegrator(login=login_instance)


HERE = Path(__file__).parent

# Files to store processed entities
PROCESSED_SPECIES_PATH = HERE / "processed_species.yaml"
PROCESSED_FAMILIES_PATH = HERE / "processed_families.yaml"
PROCESSED_GENERA_PATH = HERE / "processed_genera.yaml"
PROCESSED_FILES_PATH = HERE / "processed_files.yaml"

YAML_PATH = HERE / "categories_to_review.yaml"


def load_processed_entities(entity_type):
    """
    Load the set of processed entities (species, genera, or families) names from the YAML file.
    Returns a set of entity names (strings).
    """
    if entity_type == "species":
        path = PROCESSED_SPECIES_PATH
    elif entity_type == "families":
        path = PROCESSED_FAMILIES_PATH
    elif entity_type == "genera":
        path = PROCESSED_GENERA_PATH
    elif entity_type == "files":
        path = PROCESSED_FILES_PATH
    else:
        raise ValueError(f"Unknown entity type: {entity_type}")

    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return set(data) if data else set()
    return set()


def save_processed_entities(processed_entities: set, entity_type):
    """
    Save the set of processed entities (species, genera, or families) names to the YAML file.
    """
    if entity_type == "species":
        path = PROCESSED_SPECIES_PATH
    elif entity_type == "families":
        path = PROCESSED_FAMILIES_PATH
    elif entity_type == "genera":
        path = PROCESSED_GENERA_PATH
    elif entity_type == "files":
        path = PROCESSED_FILES_PATH
    else:
        raise ValueError(f"Unknown entity type: {entity_type}")

    with path.open("w", encoding="utf-8") as f:
        yaml.dump(
            list(processed_entities), f, default_flow_style=False, allow_unicode=True
        )


def generate_editgroup_snippet():
    # As per https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool
    random_hex = f"{random.randrange(0, 2**48):x}"
    editgroup_snippet = f"([[:toolforge:editgroups-commons/b/CB/{random_hex}|details]])"
    return editgroup_snippet


def process_family_category(category, verbose, edit_group_snippet):
    """
    CLI tool to process Wikimedia Commons categories and update Wikidata
    based on the number of images found in each category.
    """
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING)
    logging.info(f"Processing category: {category}")

    categories_to_review = {}
    # Load the already processed species names
    processed_species = load_processed_entities("species")
    # Load the already processed genera
    processed_genera = load_processed_entities("genera")

    # 1) Fetch subcategories recursively (these are the genera)
    genera = get_subcategories(category, verbose=verbose)
    logging.info(f"Top-level category has {len(genera)} subcategories.")

    # 2) Process each genus and its taxa (species)
    for genus in tqdm(genera, desc="Processing genera"):
        if "Unidentified" in genus or genus in processed_genera:
            logging.info(f"Skipping already processed genus: {genus}")
            continue

        taxa = get_subcategories(genus, verbose=verbose)
        for taxon in tqdm(taxa, desc=f"Processing taxa in {genus}", leave=False):

            # Use regex to extract the species name from the category title
            match = re.match(r"([^\\-]+) - botanical illustrations", taxon)
            if not match:
                match = re.match(r"([^\\-]+) botanical illustrations", taxon)
                if not match:
                    match = re.match(r"([^\\-]+) \(illustrations\)", taxon)
                    if not match:
                        logging.info(f"Skipping taxon {taxon}: no match for regex.")
                        continue

            species_name = match.group(1).strip()
            # Skip processing if this species has already been handled
            if species_name in processed_species:
                logging.info(f"Skipping already processed species: {species_name}")
                continue

            wikidata_item = get_qid_from_taxon_name(species_name)
            if not wikidata_item:
                # Even if no Wikidata item is found, mark species as processed to avoid rechecking it later
                processed_species.add(species_name)
                save_processed_entities(processed_species, "species")
                continue

            # 3) Get all files in the taxon category
            files = get_files_in_category(taxon, verbose=verbose)

            add_depicts_statements(files, wikidata_item, edit_group_snippet)

            # Mark species as processed and update the file
            processed_species.add(species_name)
            save_processed_entities(processed_species, "species")

        # Mark genus as processed after all its taxa are handled
        processed_genera.add(genus)
        save_processed_entities(processed_genera, "genera")

    print("Processing complete. Wikidata updated.")


def get_media_info_id(file_name):
    API_URL = "https://commons.wikimedia.org/w/api.php"
    if "File:" in file_name:
        file_name = file_name.replace("File:", "")
    params = {
        "action": "query",
        "titles": f"File:{file_name}",
        "prop": "info",
        "format": "json",
    }
    try:
        response = requests.get(API_URL, params=params)
        data = response.json()
        pages = data.get("query", {}).get("pages", {})
        if not pages:
            return "Error: No page data found for the file."
        page = next(iter(pages.values()))
        if "pageid" in page:
            media_info_id = f"M{page['pageid']}"
            return media_info_id
        else:
            return "Error: MediaInfo ID could not be found for the file."
    except requests.RequestException as e:
        return f"Error: API request failed. {e}"


def add_depicts_statements(files: list, depicted_item: str, edit_group_snippet=""):
    """
    Determine which property (P18 or P13162) should be added to a
    Wikidata item and add it in one write, if needed.
    """
    processed_files = load_processed_entities("files")
    for file_name in files:
        if file_name in processed_files:
            logging.info(f"Skipping already processed file: {file_name}")
            continue
        try:
            data = get_media_info_id(file_name)
            mediainfo_id = data
            media = wbi.mediainfo.get(entity_id=mediainfo_id)
        except Exception as e:
            if "The MW API returned that the entity was missing." in str(e):
                media = wbi.mediainfo.new(id=mediainfo_id)
            else:
                logging.error(f"Could not load MediaInfo for File:{file_name}: {e}")
                continue
        new_statements = []
        add_depicts_claim(depicted_item, new_statements, media, file_name)
        edit_summary = f"Add depicts claim for {depicted_item} {edit_group_snippet}"
        if new_statements:
            media.claims.add(
                new_statements,
                action_if_exists=wbi_enums.ActionIfExists.MERGE_REFS_OR_APPEND,
            )
            try:
                media.write(summary=edit_summary)
                tqdm.write(
                    f"No errors when trying to update {file_name} with SDC data."
                )
            except Exception as e:
                logging.error(f"Failed to write SDC for {file_name}: {e}")
        else:
            logging.info(f"No SDC data to add for {file_name}, skipping...")

        processed_files.add(file_name)
        save_processed_entities(processed_files, "files")
    logging.info("All files processed.")


def add_depicts_claim(qid, new_statements, media, file_name, set_prominent=False):

    # Get all the categories for the file:
    params = {
        "action": "query",
        "titles": f"File:{file_name}",
        "prop": "categories",
        "format": "json",
    }
    try:
        response = requests.get(
            "https://commons.wikimedia.org/w/api.php", params=params
        )
        data = response.json()
        pages = data.get("query", {}).get("pages", {})
        if not pages:
            logging.error(f"No page data found for the file: {file_name}")
            pass
        page = next(iter(pages.values()))
        if "categories" in page:
            categories = [cat["title"] for cat in page["categories"]]
            logging.info(f"Categories for {file_name}: {categories}")
        else:
            logging.error(f"No categories found for the file: {file_name}")
            pass
    except requests.RequestException as e:
        logging.error(f"API request failed for {file_name}: {e}")
        pass

    list_of_taxonomic_qids = []
    for category in categories:
        taxon_name = category.split("-")[0].strip().replace("Category:", "")
        qid_for_cat = get_qid_from_taxon_name(taxon_name)
        if qid_for_cat:
            list_of_taxonomic_qids.append(qid_for_cat)

    list_of_taxonomic_qids = list(set(list_of_taxonomic_qids))
    if len(list_of_taxonomic_qids) > 1:
        rank = "normal"
    else:
        rank = "preferred"

    claims_in_media = media.claims.get_json()

    for taxonomic_qid in list_of_taxonomic_qids:
        if "P180" in claims_in_media:
            p180_values = media.claims.get_json()["P180"]

            for value in p180_values:
                if value["mainsnak"]["datavalue"]["value"]["id"] == taxonomic_qid:
                    continue

        if taxonomic_qid:
            references = create_reference(file_name)
            claim_depicts = Item(
                prop_nr="P180", value=taxonomic_qid, references=references, rank=rank
            )
            new_statements.append(claim_depicts)


def create_reference(file_name):
    """
    Creates a reference object: 'Inferred from Wikimedia Commons' (P887=Q131478853)
    along with the file permalink.
    """
    references = References()
    ref_obj = Reference()
    ref_obj.add(Item(prop_nr="P887", value="Q131478853"))
    commons_permalink = build_commons_file_permalink(file_name)
    ref_obj.add(URL(prop_nr="P4656", value=commons_permalink))
    references.add(ref_obj)
    return references


def save_to_yaml(data: dict):
    """
    Saves categories with 3+ images to a YAML file for manual review.
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


if __name__ == "__main__":
    edit_group_snippet = generate_editgroup_snippet()
    list_of_categories = get_subcategories(
        "Botanical illustrations by family", verbose=True
    )

    families = load_processed_entities("families")
    for category in list_of_categories:
        print(f"####### Processing category: {category} #######")
        if category in families:
            print(f"Skipping already processed family: {category}")
            continue
        process_family_category(category, True, edit_group_snippet)
        families.add(category)
        save_processed_entities(families, "families")
