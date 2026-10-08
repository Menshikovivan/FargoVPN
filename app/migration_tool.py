#!/usr/bin/env python3
"""Reproducible SQLite -> PostgreSQL migration and verification tool."""
from __future__ import annotations
import argparse, copy, hashlib, json, os, sqlite3, sys, datetime as dt
from pathlib import Path
from typing import Any
import db as database_adapter

def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()

def tables(conn):
    return [str(r[0]) for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]

def schema_snapshot(conn, path: Path) -> dict[str,Any]:
    out={"source":str(path),"file_size":path.stat().st_size,"sha256":sha256_file(path),
         "sqlite_version":sqlite3.sqlite_version,"tables":{}}
    for table in tables(conn):
        cols=[]
        for r in conn.execute(f'PRAGMA table_info("{table.replace(chr(34),chr(34)*2)}")'):
            cols.append({"cid":int(r[0]),"name":str(r[1]),"type":str(r[2] or ""),
                         "notnull":int(r[3]),"default":r[4],"pk":int(r[5])})
        indexes=[]
        for r in conn.execute(f'PRAGMA index_list("{table.replace(chr(34),chr(34)*2)}")'):
            name=str(r[1]); unique=int(r[2])
            entries=[str(x[2]) for x in conn.execute(f'PRAGMA index_info("{name.replace(chr(34),chr(34)*2)}")')]
            indexes.append({"name":name,"unique":unique,"columns":entries})
        fks=[]
        for r in conn.execute(f'PRAGMA foreign_key_list("{table.replace(chr(34),chr(34)*2)}")'):
            fks.append({"id":int(r[0]),"seq":int(r[1]),"table":str(r[2]),"from":str(r[3]),"to":str(r[4]),
                        "on_update":str(r[5]),"on_delete":str(r[6])})
        row_count=int(conn.execute(f'SELECT COUNT(*) FROM "{table.replace(chr(34),chr(34)*2)}"').fetchone()[0])
        out["tables"][table]={"columns":cols,"indexes":indexes,"foreign_keys":fks,"rows":row_count}
    return out

def normalize(v:Any)->str:
    if v is None: return "NULL"
    if isinstance(v,(bytes,bytearray,memoryview)): return "B64:"+bytes(v).hex()
    if isinstance(v,bool): return "BOOL:" + ("1" if v else "0")
    if isinstance(v,float):
        return f"FLOAT:{v:.17g}"
    return "VAL:"+str(v)

def _canonical_value(v:Any)->str:
    """Canonical cross-driver representation used for exact row comparison."""
    v=normalize_pg_value(v) if 'normalize_pg_value' in globals() else v
    if v is None: return "NULL"
    if isinstance(v,(bytes,bytearray,memoryview)): return "B64:"+bytes(v).hex()
    if isinstance(v,bool): return "BOOL:"+('1' if v else '0')
    # SQLite REAL commonly arrives as float while PostgreSQL NUMERIC arrives as Decimal.
    # Preserve exact textual value for ordinary text/int fields, but normalize numeric
    # values through their string representation only when the underlying type is numeric.
    if isinstance(v,float): return f"FLOAT:{v:.17g}"
    return "VAL:"+str(v)

def _row_digest(values:list[Any])->str:
    return hashlib.sha256(("\x1f".join(_canonical_value(v) for v in values)+"\n").encode()).hexdigest()

def table_fingerprint(conn, table:str, pk_cols:list[str], chunk:int=2000)->dict[str,Any]:
    quoted='"'+table.replace('"','""')+'"'
    cols=[str(r[1]) for r in conn.execute(f'PRAGMA table_info({quoted})')]
    # Do not depend on DB-specific ORDER BY / NULL ordering. A multiset of row digests
    # detects the same data regardless of physical row order, including duplicate rows.
    cur=conn.execute(f"SELECT * FROM {quoted}")
    hrows=[]; count=0; null_counts={c:0 for c in cols}; unique_values={c:set() for c in cols}
    min_id=max_id=None
    while True:
        rows=cur.fetchmany(chunk)
        if not rows: break
        for row in rows:
            vals=[row[i] for i in range(len(cols))]
            hrows.append(_row_digest(vals))
            count+=1
            for i,c in enumerate(cols):
                if vals[i] is None: null_counts[c]+=1
                elif len(unique_values[c])<100000: unique_values[c].add(_canonical_value(vals[i]))
            if 'id' in cols:
                v=vals[cols.index('id')]
                try:
                    min_id=v if min_id is None else min(min_id,v)
                    max_id=v if max_id is None else max(max_id,v)
                except Exception: pass
    hrows.sort()
    h=hashlib.sha256(); [h.update(x.encode()) for x in hrows]
    return {'rows':count,'min_id':min_id,'max_id':max_id,'null_counts':null_counts,
            'unique_counts':{k:len(v) for k,v in unique_values.items()},'sha256':h.hexdigest()}

def sqlite_to_pg_type(t:str, autoinc:bool=False, pk:bool=False)->str:
    u=(t or "").upper()
    if "INT" in u: return "BIGINT"
    if any(x in u for x in ("CHAR","CLOB","TEXT")): return "TEXT"
    if "BLOB" in u: return "BYTEA"
    if any(x in u for x in ("REAL","FLOA","DOUB")): return "DOUBLE PRECISION"
    if "NUMERIC" in u or "DECIMAL" in u: return "NUMERIC"
    if "BOOL" in u: return "BOOLEAN"
    if "DATE" in u or "TIME" in u: return "TEXT"
    return "TEXT"

def create_target_table(conn_pg, sqlite_conn, table:str, snap:dict[str,Any]):
    meta=snap["tables"][table]
    source_sql=sqlite_conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()[0] or ""
    autoinc="AUTOINCREMENT" in source_sql.upper()
    parts=[]
    pk=[c for c in meta["columns"] if c["pk"]]
    for c in meta["columns"]:
        name='"'+c["name"].replace('"','""')+'"'
        typ=sqlite_to_pg_type(c["type"],autoinc, bool(c["pk"]))
        # Preserve SQLite physical column order. AUTOINCREMENT id is defined
        # in-place instead of being removed and appended later.
        typ_sql = ("BIGINT GENERATED BY DEFAULT AS IDENTITY"
                   if autoinc and c["pk"] and c["name"] == "id" else typ)
        # SQLite boolean-ish INTEGER columns that are explicit enable/success/etc
        # stay BIGINT unless the source schema says otherwise; application code
        # tolerates both and verification normalizes values.
        nullable="" if c["notnull"]==0 or c["pk"] else " NOT NULL"
        default=c["default"]
        default_sql=""
        if default is not None:
            ds=str(default)
            if ds.upper()=="CURRENT_TIMESTAMP": default_sql=" DEFAULT CURRENT_TIMESTAMP"
            elif autoinc and c["pk"]: default_sql=""
            elif ds in ("0","1","-1"): default_sql=f" DEFAULT {ds}"
            elif ds.upper() in ("TRUE","FALSE"): default_sql=f" DEFAULT {ds.upper()}"
        parts.append(f"{name} {typ_sql}{nullable}{default_sql}")
    if len(pk)==1:
        pk_name=pk[0]["name"].replace(chr(34),chr(34)*2)
        parts.append(f'PRIMARY KEY ("{pk_name}")')
    elif pk:
        cols=", ".join('"'+c["name"].replace('"','""')+'"' for c in pk)
        parts.append(f"PRIMARY KEY ({cols})")
    for fk in meta["foreign_keys"]:
        parts.append(
            f'FOREIGN KEY ("{fk["from"].replace(chr(34),chr(34)*2)}") '
            f'REFERENCES "{fk["table"].replace(chr(34),chr(34)*2)}" ("{fk["to"].replace(chr(34),chr(34)*2)}") '
            f'ON UPDATE {fk["on_update"]} ON DELETE {fk["on_delete"]}'
        )
    ddl=f'CREATE TABLE IF NOT EXISTS "{table.replace(chr(34),chr(34)*2)}" ('+", ".join(parts)+")"
    conn_pg.execute(ddl)

def recreate_indexes(conn_pg, snap:dict[str,Any]):
    """Recreate SQLite secondary indexes, including named UNIQUE indexes."""
    for table, meta in snap["tables"].items():
        for idx in meta.get("indexes", []):
            name=str(idx["name"])
            if name.startswith("sqlite_autoindex_"):
                continue
            cols=idx.get("columns") or []
            if not cols:
                continue
            qtable=table.replace(chr(34), chr(34)*2)
            qname=name.replace(chr(34), chr(34)*2)
            qcols=", ".join('"'+c.replace(chr(34),chr(34)*2)+'"' for c in cols)
            unique="UNIQUE " if int(idx.get("unique",0)) else ""
            conn_pg.execute(f'CREATE {unique} INDEX IF NOT EXISTS "{qname}" ON "{qtable}" ({qcols})')

def recreate_sequences(conn_pg, snap:dict[str,Any]):
    for table, meta in snap["tables"].items():
        cols=meta["columns"]
        pk=[c for c in cols if c["pk"]]
        if len(pk)!=1 or pk[0]["name"]!="id":
            continue
        # Only identity columns have a PostgreSQL sequence.
        try:
            conn_pg.execute(
                f"SELECT setval(pg_get_serial_sequence('\"{table.replace(chr(34),chr(34)*2)}\"','id'),"
                f"COALESCE(MAX(\"id\"), 1), MAX(\"id\") IS NOT NULL) FROM \"{table.replace(chr(34),chr(34)*2)}\""
            )
        except Exception:
            pass

def row_dicts_sqlite(conn, table):
    q='"'+table.replace('"','""')+'"'
    cur=conn.execute(f"SELECT * FROM {q}")
    cols=[d[0] for d in cur.description]
    while True:
        rows=cur.fetchmany(1000)
        if not rows: return
        for r in rows:
            yield cols,r

def insert_rows(pg, table, cols, rows):
    """Insert a batch without suppressing conflicts.

    The migration target is isolated and must preserve every source row.
    Any duplicate/conflict must fail loudly and be recorded by migrate().
    """
    qs=", ".join("?" for _ in cols)
    quoted=", ".join('"'+c.replace('"','""')+'"' for c in cols)
    sql=f'INSERT INTO "{table.replace(chr(34),chr(34)*2)}" ({quoted}) VALUES ({qs})'
    pg.executemany(sql, [tuple(r) for r in rows])


def normalize_pg_value(v:Any)->Any:
    if isinstance(v,(dt.datetime,dt.date,dt.time)):
        return v.isoformat()
    if isinstance(v,memoryview): return "B64:"+bytes(v).hex()
    if isinstance(v,(bytes,bytearray)): return "B64:"+bytes(v).hex()
    if isinstance(v,bool): return int(v)
    return v

def pg_fingerprint(pg, table:str, pk_cols:list[str], cols:list[str]) -> dict[str,Any]:
    # Same order-independent multiset fingerprint as the SQLite side.
    # PostgreSQL physical column order can differ from SQLite (for example
    # when an identity column was appended by an earlier migration build).
    # Select columns explicitly in the source schema order so verification
    # compares the same fields by position.
    qcols=", ".join('\"'+c.replace('\"','\"\"')+'\"' for c in cols)
    q=f'SELECT {qcols} FROM "{table.replace(chr(34),chr(34)*2)}"'
    cur=pg.execute(q); row_digests=[]; count=0
    null_counts={c:0 for c in cols}; unique_values={c:set() for c in cols}; min_id=max_id=None
    while True:
        rows=cur.fetchmany(2000)
        if not rows: break
        for row in rows:
            vals=[normalize_pg_value(row[i]) for i in range(len(cols))]
            row_digests.append(_row_digest(vals)); count+=1
            for i,c in enumerate(cols):
                v=vals[i]
                if v is None: null_counts[c]+=1
                elif len(unique_values[c])<100000: unique_values[c].add(_canonical_value(v))
            if 'id' in cols:
                try:
                    v=vals[cols.index('id')]; min_id=v if min_id is None else min(min_id,v); max_id=v if max_id is None else max(max_id,v)
                except Exception: pass
    row_digests.sort(); h=hashlib.sha256()
    for digest in row_digests: h.update(digest.encode())
    return {'rows':count,'min_id':min_id,'max_id':max_id,'null_counts':null_counts,
            'unique_counts':{k:len(v) for k,v in unique_values.items()},'sha256':h.hexdigest()}

def row_level_diffs(sqlite_conn, pg, table:str, pk_cols:list[str], cols:list[str], limit:int=100)->list[dict[str,Any]]:
    """Stream both ordered tables and report concrete differing/missing rows."""
    if not pk_cols:
        return []
    qcols=', '.join('"'+c.replace('"','""')+'"' for c in cols)
    order=', '.join('"'+c.replace('"','""')+'"' for c in pk_cols)
    sq=sqlite_conn.execute(f'SELECT {qcols} FROM "{table.replace(chr(34),chr(34)*2)}" ORDER BY {order}')
    pq=pg.execute(f'SELECT {qcols} FROM "{table.replace(chr(34),chr(34)*2)}" ORDER BY {order}')
    diffs=[]
    sa=sq.fetchmany(2000); pa=pq.fetchmany(2000); i=j=0
    while (sa or pa) and len(diffs)<limit:
        if i>=len(sa): sa=sq.fetchmany(2000); i=0
        if j>=len(pa): pa=pq.fetchmany(2000); j=0
        if not sa and not pa: break
        sr=sa[i] if i<len(sa) else None; pr=pa[j] if j<len(pa) else None
        if sr is None:
            diffs.append({"type":"missing_in_sqlite","row":{cols[k]:normalize_pg_value(pr[k]) for k in range(len(cols))}}); j+=1; continue
        if pr is None:
            diffs.append({"type":"missing_in_postgresql","row":{cols[k]:sr[k] for k in range(len(cols))}}); i+=1; continue
        sk=tuple(sr[cols.index(c)] for c in pk_cols); pk=tuple(pr[cols.index(c)] for c in pk_cols)
        if sk==pk:
            for k,c in enumerate(cols):
                a=normalize(sr[k]); b=normalize(normalize_pg_value(pr[k]))
                if a!=b:
                    diffs.append({"type":"field_mismatch","primary_key":{pc:sr[cols.index(pc)] for pc in pk_cols},"field":c,"sqlite":sr[k],"postgresql":normalize_pg_value(pr[k])})
                    if len(diffs)>=limit: break
            i+=1; j+=1
        elif sk<pk:
            diffs.append({"type":"missing_in_postgresql","primary_key":{pc:sr[cols.index(pc)] for pc in pk_cols},"row":{cols[k]:sr[k] for k in range(len(cols))}}); i+=1
        else:
            diffs.append({"type":"missing_in_sqlite","primary_key":{pc:pr[cols.index(pc)] for pc in pk_cols},"row":{cols[k]:normalize_pg_value(pr[k]) for k in range(len(cols))}}); j+=1
    return diffs

def _write_json(path: Path, payload: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _safe_error(exc: BaseException) -> dict[str, Any]:
    import traceback
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }


def migrate(sqlite_path:Path, dsn:str, manifest_path:Path, report_path:Path, reset:bool=False,
            failure_report_path:Path|None=None)->dict[str,Any]:
    """Migrate SQLite -> PostgreSQL with durable failure diagnostics.

    The report is written incrementally and, on failure, additionally copied to
    failure_report_path, which is expected to live outside the install target so
    installer rollback cannot delete the evidence.
    """
    sconn=None
    pg=None
    old_url=os.environ.get("FARGOVPN_DATABASE_URL") or os.environ.get("DATABASE_URL")
    stage="open_sqlite"
    current_table=None
    report={
        "generated_at":dt.datetime.now(dt.timezone.utc).isoformat(),
        "source":str(sqlite_path),
        "target":dsn.split("@")[ -1 ],
        "status":"RUNNING",
        "stage":stage,
        "current_table":None,
        "tables":{},
    }

    def persist():
        _write_json(report_path, report)
        if failure_report_path is not None:
            _write_json(failure_report_path, report)

    try:
        sconn=sqlite3.connect(str(sqlite_path)); sconn.row_factory=sqlite3.Row
        snap=schema_snapshot(sconn,sqlite_path)
        for t,meta in snap["tables"].items():
            pk=[c["name"] for c in meta["columns"] if c["pk"]]
            meta["fingerprint"]=table_fingerprint(sconn,t,pk)
        report["source_sha256"]=snap["sha256"]
        report["source_file_size"]=snap["file_size"]
        report["source_tables"]=len(snap["tables"])
        report["source_rows"]=sum(int(m["fingerprint"]["rows"]) for m in snap["tables"].values())
        stage="manifest"
        report["stage"]=stage
        persist()
        _write_json(manifest_path, snap)

        stage="connect_postgresql"
        report["stage"]=stage
        persist()
        os.environ["FARGOVPN_DATABASE_URL"]=dsn
        os.environ["DATABASE_URL"]=dsn
        database_adapter.dispose()
        pg=database_adapter.connect()
        pg.execute("CREATE SCHEMA IF NOT EXISTS public")
        pg.execute("SET search_path TO public")

        if reset:
            stage="reset_target"
            report["stage"]=stage
            persist()
            for t in reversed(list(snap["tables"])):
                current_table=t; report["current_table"]=t; persist()
                pg.execute(f'DROP TABLE IF EXISTS "{t.replace(chr(34),chr(34)*2)}" CASCADE')

        stage="create_tables"
        report["stage"]=stage
        persist()
        for t in snap["tables"]:
            current_table=t; report["current_table"]=t; persist()
            create_target_table(pg,sconn,t,snap)

        stage="insert_rows"
        report["stage"]=stage
        persist()
        for t in snap["tables"]:
            current_table=t; report["current_table"]=t; report["table_source_rows"]=snap["tables"][t]["fingerprint"]["rows"]; persist()
            cols=[c["name"] for c in snap["tables"][t]["columns"]]
            batch=[]
            for _, row in row_dicts_sqlite(sconn,t):
                batch.append(tuple(row))
                if len(batch)>=1000:
                    insert_rows(pg,t,cols,batch); batch=[]
            if batch: insert_rows(pg,t,cols,batch)
            report["tables"].setdefault(t,{})["inserted_rows_expected"]=snap["tables"][t]["fingerprint"]["rows"]
            persist()

        stage="recreate_indexes"
        report["stage"]=stage; report["current_table"]=None; persist()
        recreate_indexes(pg, snap)

        stage="recreate_sequences"
        report["stage"]=stage; persist()
        recreate_sequences(pg, snap)

        stage="verify"
        report["stage"]=stage; report["current_table"]=None; persist()
        report["tables"]={}
        for t,meta in snap["tables"].items():
            current_table=t; report["current_table"]=t; persist()
            cols=[c["name"] for c in meta["columns"]]; pk=[c["name"] for c in meta["columns"] if c["pk"]]
            pgfp=pg_fingerprint(pg,t,pk,cols)
            diff={"row_difference":pgfp["rows"]-meta["fingerprint"]["rows"],
                  "source_sha256":meta["fingerprint"]["sha256"],"target_sha256":pgfp["sha256"]}
            status=diff["row_difference"]==0 and diff["source_sha256"]==diff["target_sha256"]
            if not status:
                diff["row_level_diffs"]=row_level_diffs(sconn, pg, t, pk, cols)
                # A row-level comparison with a primary key is authoritative when
                # fingerprints disagree but every row/field is equal. This avoids
                # false negatives caused by backend ordering/canonicalization.
                if diff["row_level_diffs"]==[] and pk and diff["row_difference"]==0:
                    status=True
            report["tables"][t]={"sqlite":meta["fingerprint"],"postgresql":pgfp,"difference":diff,"status":"PASS" if status else "FAIL"}
            if not status:
                report["status"]="FAIL"
                report["stage"]="verify_failed"
                persist()
                raise RuntimeError(f"Verification failed for table {t}: {diff}")
            persist()

        # The migration uses one PostgreSQL transaction for the whole import.
        # Verification above intentionally reads that same transaction, so it
        # can see all inserted rows. Before returning successfully we MUST
        # commit it. Closing a SQLAlchemy connection with an open transaction
        # otherwise rolls the imported data back, leaving an apparently valid
        # but empty PostgreSQL database after the installer finishes.
        stage="commit"
        report["stage"]=stage
        report["current_table"]=None
        persist()
        pg.commit()

        # Re-check the target after commit using a fresh connection. This turns
        # a successful transaction into a durable verification rather than only
        # an in-transaction verification.
        pg.close()
        pg = database_adapter.connect()
        pg.execute("SET search_path TO public")
        stage="verify_committed"
        report["stage"]=stage
        persist()
        for t,meta in snap["tables"].items():
            current_table=t
            cols=[c["name"] for c in meta["columns"]]
            pk=[c["name"] for c in meta["columns"] if c["pk"]]
            pgfp=pg_fingerprint(pg,t,pk,cols)
            diff={"row_difference":pgfp["rows"]-meta["fingerprint"]["rows"],
                  "source_sha256":meta["fingerprint"]["sha256"],"target_sha256":pgfp["sha256"]}
            status=diff["row_difference"]==0 and diff["source_sha256"]==diff["target_sha256"]
            if not status:
                diff["row_level_diffs"]=row_level_diffs(sconn, pg, t, pk, cols)
                if diff["row_level_diffs"]==[] and pk and diff["row_difference"]==0:
                    status=True
            report["committed_verification"] = report.get("committed_verification", {})
            report["committed_verification"][t] = {
                "sqlite": meta["fingerprint"],
                "postgresql": pgfp,
                "difference": diff,
                "status": "PASS" if status else "FAIL",
            }
            persist()
            if not status:
                report["status"]="FAIL"
                report["stage"]="verify_committed_failed"
                persist()
                raise RuntimeError(f"Post-commit verification failed for table {t}: {diff}")

        report["current_table"]=None
        report["stage"]="completed"
        report["status"]="PASS"
        report["completed_at"]=dt.datetime.now(dt.timezone.utc).isoformat()
        persist()
        return report
    except Exception as exc:
        report["status"]="FAIL"
        report["stage"]=stage
        report["current_table"]=current_table
        report["error"]=_safe_error(exc)
        report["failed_at"]=dt.datetime.now(dt.timezone.utc).isoformat()
        persist()
        if pg is not None:
            try: pg.rollback()
            except Exception: pass
        raise
    finally:
        try:
            if pg is not None: pg.close()
        except Exception: pass
        try:
            if sconn is not None: sconn.close()
        except Exception: pass
        if old_url: os.environ["FARGOVPN_DATABASE_URL"]=old_url
        else: os.environ.pop("FARGOVPN_DATABASE_URL",None)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sqlite",required=True)
    ap.add_argument("--dsn",required=False)
    ap.add_argument("--manifest",default="migration_manifest.json")
    ap.add_argument("--report",default="migration_report.json")
    ap.add_argument("--reset-target",action="store_true")
    ap.add_argument("--manifest-only",action="store_true")
    ap.add_argument("--failure-report", default="")
    args=ap.parse_args()
    path=Path(args.sqlite).resolve()
    if not path.is_file(): raise SystemExit(f"SQLite DB not found: {path}")
    conn=sqlite3.connect(str(path))
    snap=schema_snapshot(conn,path)
    for t,meta in snap["tables"].items():
        meta["fingerprint"]=table_fingerprint(conn,t,[c["name"] for c in meta["columns"] if c["pk"]])
    Path(args.manifest).write_text(json.dumps(snap,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    conn.close()
    print(f"MANIFEST: {args.manifest}")
    print(f"SHA256: {snap['sha256']}")
    print(f"TABLES: {len(snap['tables'])}")
    print("ROWS: "+str(sum(x["fingerprint"]["rows"] for x in snap["tables"].values())))
    if args.manifest_only: return 0
    if not args.dsn: raise SystemExit("--dsn is required unless --manifest-only is used")
    failure_report=Path(args.failure_report) if args.failure_report else None
    report=migrate(path,args.dsn,Path(args.manifest),Path(args.report),args.reset_target,failure_report)
    print(json.dumps({"status":report["status"],"report":args.report},ensure_ascii=False))
    return 0 if report["status"]=="PASS" else 2

if __name__=="__main__": raise SystemExit(main())
