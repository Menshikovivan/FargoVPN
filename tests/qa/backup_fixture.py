"""Offline PostgreSQL COPY data reader; never executes backup SQL/config/code."""
import re
import sqlite3
from pathlib import Path


def decode_copy(value):
    if value == r'\N':
        return None
    escapes={'t':'\t','n':'\n','r':'\r','b':'\b','f':'\f','v':'\v','\\':'\\'}
    def unescape(match):
        token=match.group(1)
        return chr(int(token,8)) if re.fullmatch('[0-7]{1,3}',token) else escapes.get(token,token)
    return re.sub(r'\\([0-7]{1,3}|.)',unescape,value)


def build_fixture(sql_path, destination):
    text=Path(sql_path).read_text(encoding='utf-8')
    conn=sqlite3.connect(destination)
    counts={}
    types={}
    primary={table:cols.split(', ') for table,cols in re.findall(r'ALTER TABLE ONLY public\.([\w]+)\s+ADD CONSTRAINT [\w]+ PRIMARY KEY \(([^)]+)\);',text)}
    for table,body in re.findall(r'CREATE TABLE public\.([\w]+) \(\n(.*?)\n\);',text,re.S):
        columns=[];types[table]={}
        for line in body.splitlines():
            match=re.match(r'\s*([a-zA-Z_][\w]*)\s+(bigint|integer|smallint|boolean|double precision|numeric|real|text|character varying|timestamp[^,]*|jsonb|json)',line)
            if not match:raise ValueError('Unsupported dump column declaration')
            name,kind=match.groups();types[table][name]=kind
            sqlite_kind='INTEGER' if kind in ('bigint','integer','smallint','boolean') else 'REAL' if kind in ('double precision','numeric','real') else 'TEXT'
            columns.append('"'+name+'" '+sqlite_kind)
        if table in primary:columns.append('PRIMARY KEY ('+','.join('"'+x+'"' for x in primary[table])+')')
        conn.execute('CREATE TABLE "'+table+'" ('+','.join(columns)+')')
    lines=text.splitlines();i=0
    while i<len(lines):
        match=re.match(r'^COPY public\.([\w]+) \(([^)]+)\) FROM stdin;$',lines[i]);i+=1
        if not match:continue
        table,columns=match.groups();columns=columns.split(', ');rows=[]
        while i<len(lines) and lines[i]!=r'\.':
            fields=[decode_copy(x) for x in lines[i].split('\t')];i+=1
            if len(fields)!=len(columns):raise ValueError('COPY column count mismatch')
            for index,column in enumerate(columns):
                if types[table][column]=='boolean' and fields[index] is not None:fields[index]=1 if fields[index]=='t' else 0
            rows.append(fields)
        if i>=len(lines):raise ValueError('Incomplete COPY')
        counts[table]=len(rows)
        conn.executemany('INSERT INTO "'+table+'" ('+','.join('"'+x+'"' for x in columns)+') VALUES ('+','.join('?' for _ in columns)+')',rows)
    conn.commit();conn.close()
    return counts
