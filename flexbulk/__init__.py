"""Flex Bulk: the FlexSearch bulk-order matcher on its own, for Webb staff matching customers' lists against a new
item list (items, branch stock and prices loaded from files).

    flexbulk.loader   the item, stock and price files -> the tables the matcher reads (config/bulk_sources.yaml)
    flexbulk.build    load, then build the search index (python -m flexbulk.build)
    flexbulk.orders   saved orders: customer, job, the matched lines; export to Excel or CSV
    flexbulk.server   the staff app and the settings dashboard (uvicorn flexbulk.server:app)

The matching itself (webbsearch.retrieval.bom and everything under it) is FlexSearch's, copied, not imported.
"""
