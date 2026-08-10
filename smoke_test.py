"""
Smoke test: verify Neo4j connection only. No LLM calls.
Run: python smoke_test.py
For full LLM pipeline test, run: python integration_test.py
"""
import os
import sys
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")


def main():
    print(f"[1/3] Connecting to Neo4j at {NEO4J_URI}...")
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        print("[2/3] Driver created. Verifying connectivity...")
        driver.verify_connectivity()
        print("[3/3] Connected successfully.")
        with driver.session() as session:
            result = session.run("RETURN 'menhir online' AS msg")
            record = result.single()
            print(f"Neo4j says: {record['msg']}")
        driver.close()
        print("Done. Neo4j is up and reachable.")
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
