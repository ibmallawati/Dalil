"""
chatbot.scraper — corpus acquisition. BUILD-TIME ONLY.

Nothing in the request path imports this package. It exists so
scripts/scrape_corpus.py has a testable home for the extraction
validator, which is the component that decides whether generated content
is allowed to become corpus.
"""
