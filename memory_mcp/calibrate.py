"""Prints how similar real embeddings rate example pairs, to tune the duplicate/conflict thresholds."""
from fastembed import TextEmbedding
import numpy as np

pairs = {
    "duplicate":  [("we use Postgres", "the database is Postgres"),
                   ("tests need DOCKER_HOST set", "you have to set DOCKER_HOST before running tests"),
                   ("use pnpm, not npm", "this repo uses pnpm instead of npm")],
    "contradict": [("we use Postgres", "we migrated to MySQL"),
                   ("use pnpm, not npm", "use npm for this project"),
                   ("API timeout is 30 seconds", "API timeout is 60 seconds")],
    "related":    [("tests need DOCKER_HOST set", "integration tests need a running Redis"),
                   ("we use Postgres", "migrations live in db/migrations")],
    "unrelated":  [("we use Postgres", "the frontend uses Tailwind"),
                   ("tests need DOCKER_HOST set", "deploys go through GitHub Actions")],
}
model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
for kind, ps in pairs.items():
    for a, b in ps:
        va, vb = [np.array(v) for v in model.embed([a, b])]
        print(f"{kind:<11} {va @ vb / np.linalg.norm(va) / np.linalg.norm(vb):.3f}  {a!r} vs {b!r}")
