import requests
from SPARQLWrapper import SPARQLWrapper, JSON
# Base URLs

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"


# Fetch subcategories from Commons
def get_subcategories(category, verbose=False):
    params = {
        "action": "query",
        "format": "json",
        "list": "categorymembers",
        "cmtitle": f"Category:{category}",
        "cmtype": "subcat",
        "cmlimit": "max",
    }
    response = requests.get(COMMONS_API, params=params).json()
    subcategories = [cat["title"] for cat in response.get("query", {}).get("categorymembers", [])]
    if verbose:
        print(f"Found {len(subcategories)} subcategories under {category}.")
    # Remove Category: prefix
    subcategories = [sub.replace("Category:", "") for sub in subcategories]
    return subcategories

# Get file count for a category
def get_file_count(category, verbose=False):
    params = {
        "action": "query",
        "format": "json",
        "list": "categorymembers",
        "cmtitle": f"Category:{category}",
        "cmtype": "file",
        "cmlimit": "max",
    }
    response = requests.get(COMMONS_API, params=params).json()
    files = response.get("query", {}).get("categorymembers", [])
    if verbose:
        print(f"Found {len(files)} files in {category}.")
    return len(files)

# Fetch Wikidata item by taxon name
def fetch_wikidata_item(taxon_name, verbose=False):
    params = {
        "action": "wbsearchentities",
        "format": "json",
        "search": taxon_name,
        "language": "en",
        "type": "item",
        "props": "descriptions|aliases",
    }
    response = requests.get(WIKIDATA_API, params=params).json()
    for item in response.get("search", []):
        if verbose:
            print(f"Found Wikidata item for {taxon_name}: {item['id']}.")
        return item["id"]
    if verbose:
        print(f"No Wikidata item found for {taxon_name}.")
    return None

# Fetch file names from a category
def get_files_in_category(category, verbose=False):
    params = {
        "action": "query",
        "format": "json",
        "list": "categorymembers",
        "cmtitle": f"Category:{category}",
        "cmtype": "file",
        "cmlimit": "max",
    }
    response = requests.get(COMMONS_API, params=params).json()
    files = [file["title"].replace("File:", "") for file in response.get("query", {}).get("categorymembers", [])]
    if verbose:
        print(f"Found {len(files)} files in {category}: {files}")
    return files

# Fetch M-ID for a file on Wikimedia Commons
def fetch_m_id(filename, verbose=False):
    params = {
        "action": "query",
        "format": "json",
        "titles": f"File:{filename}",
    }
    response = requests.get(COMMONS_API, params=params).json()
    pages = response.get("query", {}).get("pages", {})
    for page_id, page_data in pages.items():
        if "pageid" in page_data:
            m_id = f"M{page_data['pageid']}"
            if verbose:
                print(f"Found M-ID for {filename}: {m_id}.")
            return m_id
    if verbose:
        print(f"No M-ID found for {filename}.")
    return None

# Check for P18 (image) values in batch
def check_missing_p18(wikidata_ids, verbose=False):
    sparql = SPARQLWrapper(SPARQL_ENDPOINT)
    ids_str = " ".join(f"wd:{qid}" for qid in wikidata_ids)
    query = f"""
    SELECT ?item WHERE {{
        VALUES ?item {{ {ids_str} }}
        FILTER NOT EXISTS {{ ?item wdt:P18 ?image }}
    }}
    """
    sparql.setQuery(query)
    sparql.setReturnFormat(JSON)
    results = sparql.query().convert()

    missing_p18 = set()
    for result in results["results"]["bindings"]:
        missing_p18.add(result["item"]["value"].split("/")[-1])  # Extract QID
    if verbose:
        print(f"Missing P18 for {len(missing_p18)} items: {missing_p18}.")
    return missing_p18
