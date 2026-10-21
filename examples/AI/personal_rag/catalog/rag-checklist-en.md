# Retrieval-Augmented Generation Checklist

A short operational checklist for maintaining a document retrieval
pipeline. Written in English on purpose: the catalog is bilingual, and
the search must work across both languages.

## Indexing pipeline

1. Extract text from the source file, preserving headings.
2. Split the text into chunks of about 1200 characters with a 100
   character overlap, so that a question falling on a chunk boundary
   still hears both halves.
3. Lemmatize every token: Snowball stems for English, pymorphy3 for
   Russian. Lemmas go into the FTS index, the original text stays
   untouched so quotes remain verbatim.
4. Embed each chunk with a multilingual model and store the vector in
   the vec0 table.

## Query pipeline

The query is answered by two independent rankers. The vector ranker
measures cosine similarity and is insensitive to wording; the BM25
ranker over lemmas catches exact terms, names, and code fragments.

The two ranked lists are merged with reciprocal rank fusion: every
candidate accumulates `w / (60 + rank)` from each list. Fusion needs no
score calibration, because ranks are comparable while raw scores are
not.

## Failure modes worth knowing

- A scanned PDF yields no text at all. Run it through OCR first, or the
  indexer will silently store an empty document.
- A document indexed under a model with a different vector dimension
  cannot be queried with the current model. The dimension is recorded
  when the database is created; a mismatch is reported as an error, not
  as a silent empty result.
- Russian queries do not match inflected forms in a plain FTS5 index.
  This is why lemmatization is mandatory on both sides of the match.


English appendix: the ingest timeout lives in config.toml under [llm].timeout.
