"""Build the Laws of the Game search index in Azure AI Search.

Reads the IFAB PDF from the laws/ folder, makes one search document per PDF page,
labels each page with the section it belongs to ("Law 12", "VAR protocol", ...),
uploads everything, then runs two test queries.

Usage (from the repo root):
    py -m pip install pypdf azure-search-documents python-dotenv
    py laws/build_index.py            # build the index and test it
    py laws/build_index.py --dry-run  # only print the section of each page, send nothing
"""
import glob
import os
import re
import sys
import time

from pypdf import PdfReader

LAWS_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX", "laws-of-the-game")

# Every page of a Law carries the running header "Laws of the Game 2025/26 | Law 12 | Fouls and Misconduct".
# Matching that exact header avoids cross-references such as "(see Law 3)" in the body text.
LAW_HEADER = re.compile(r"Laws of the Game 2025/26 \| Law (\d{1,2}) \|")

# The parts of the booklet after the 17 Laws, recognised by their opening words.
OTHER_SECTIONS = [
    ("Video assistant referee (VAR) protocol", "VAR protocol"),
    ("FIFA Quality Programme", "FIFA Quality Programme"),
    ("Law changes 2025/26", "Law changes 2025/26"),
    ("The IFAB – The International Football Association Board", "Glossary"),
    ("Practical guidelines for match officials", "Practical guidelines"),
]

MIN_TEXT = 100  # skip covers, blank pages and the empty "Notes" pages at the end


def find_pdf() -> str:
    pdfs = glob.glob(os.path.join(LAWS_DIR, "*.pdf"))
    if not pdfs:
        sys.exit(f"No PDF found in {LAWS_DIR}. Put the IFAB Laws of the Game PDF there.")
    if len(pdfs) > 1:
        sys.exit(f"Several PDFs in {LAWS_DIR}, keep only the Laws of the Game: {pdfs}")
    return pdfs[0]


def read_pages(pdf_path: str) -> list[dict]:
    """One document per PDF page, each labelled with its section."""
    docs = []
    section = "Introduction"  # everything before Law 1: about the Laws, notes and modifications
    after_laws = False
    next_other = 0  # the sections after Law 17 come in a fixed order; only look for the next one
    for number, page in enumerate(PdfReader(pdf_path).pages, start=1):
        text = re.sub(r"\s+", " ", page.extract_text() or "").strip()

        law = LAW_HEADER.search(text)
        if law and 1 <= int(law.group(1)) <= 17 and not after_laws:
            section = f"Law {int(law.group(1))}"
        elif section == "Law 17" or after_laws:
            if next_other < len(OTHER_SECTIONS) and OTHER_SECTIONS[next_other][0] in text[:250]:
                section = OTHER_SECTIONS[next_other][1]
                after_laws = True
                next_other += 1
        # Otherwise the page continues the previous section (Law pages without a header, e.g. diagrams).

        if len(text) < MIN_TEXT:
            continue
        docs.append({"id": f"p{number:03d}", "page": number, "section": section, "text": text})
    return docs


def print_sections(docs: list[dict]) -> None:
    """Show the page range of each section, to check the labelling at a glance."""
    ranges: list[list] = []
    for d in docs:
        if ranges and ranges[-1][0] == d["section"]:
            ranges[-1][2] = d["page"]
        else:
            ranges.append([d["section"], d["page"], d["page"]])
    for name, first, last in ranges:
        pages = f"page {first}" if first == last else f"pages {first}-{last}"
        print(f"  {name:<24} {pages}")


def main() -> None:
    pdf_path = find_pdf()
    docs = read_pages(pdf_path)
    print(f"Read {len(docs)} pages from {os.path.basename(pdf_path)}\n")
    print_sections(docs)

    if "--dry-run" in sys.argv:
        return

    # Azure imports here so that --dry-run works without the Azure libraries or the .env file.
    from azure.core.credentials import AzureKeyCredential
    from azure.core.exceptions import ResourceNotFoundError
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient
    from azure.search.documents.indexes.models import (
        SearchableField,
        SearchFieldDataType,
        SearchIndex,
        SimpleField,
    )
    from dotenv import load_dotenv

    load_dotenv()
    endpoint = os.environ["AZURE_SEARCH_ENDPOINT"]
    key = AzureKeyCredential(os.environ["AZURE_SEARCH_KEY"])

    # Start from a clean index: Azure cannot change the fields of an existing index.
    index_client = SearchIndexClient(endpoint, key)
    try:
        index_client.delete_index(INDEX_NAME)
    except ResourceNotFoundError:
        pass
    index_client.create_index(
        SearchIndex(
            name=INDEX_NAME,
            fields=[
                SimpleField(name="id", type=SearchFieldDataType.String, key=True),
                SimpleField(name="page", type=SearchFieldDataType.Int32, filterable=True, sortable=True),
                SimpleField(name="section", type=SearchFieldDataType.String, filterable=True, facetable=True),
                SearchableField(name="text", type=SearchFieldDataType.String, analyzer_name="en.lucene"),
            ],
        )
    )

    search = SearchClient(endpoint, INDEX_NAME, key)
    results = search.upload_documents(docs)
    ok = sum(1 for r in results if r.succeeded)
    print(f"\nUploaded {ok}/{len(docs)} pages to index '{INDEX_NAME}'")

    # New documents take a few seconds to become searchable, so wait for them.
    print("Waiting for the index to be ready", end="", flush=True)
    for _ in range(30):
        if search.get_document_count() >= ok:
            break
        print(".", end="", flush=True)
        time.sleep(1)
    print(" ready")

    for query, expected in [("handles the ball hand arm", "Law 12"), ("offside position", "Law 11")]:
        print(f"\nTest query: '{query}' (expected {expected})")
        for hit in search.search(query, top=3, select=["page", "section", "text"]):
            print(f"  page {hit['page']:>3}  {hit['section']:<8}  {hit['text'][:80]}...")


if __name__ == "__main__":
    main()
