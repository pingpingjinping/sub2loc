"""Read framed JSON records without changing decoded ID/text string values."""
import json
import re


def unique_object(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError('duplicate JSON key')
        value[key]=item
    return value


DECODER=json.JSONDecoder(object_pairs_hook=unique_object)


def repair_row_tail(line):
    """Allow only a trailing comma or orphan quote after two complete strings.

    Keys must be exactly id/text, once each; both values are decoded by JSON's
    string parser. Never insert quotes, alter escapes, or touch subtitle text.
    """
    line=line.strip()
    if not line.startswith('{'):return None
    cursor=1;values={}
    for number in range(2):
        while cursor<len(line) and line[cursor].isspace():cursor+=1
        try:key,cursor=DECODER.raw_decode(line,cursor)
        except ValueError:return None
        if not isinstance(key,str) or key not in ('id','text') or key in values:return None
        while cursor<len(line) and line[cursor].isspace():cursor+=1
        if line[cursor:cursor+1]!=':':return None
        cursor+=1
        while cursor<len(line) and line[cursor].isspace():cursor+=1
        try:value,cursor=DECODER.raw_decode(line,cursor)
        except ValueError:return None
        if not isinstance(value,str):return None
        values[key]=value
        if number==0:
            while cursor<len(line) and line[cursor].isspace():cursor+=1
            if line[cursor:cursor+1]!=',':return None
            cursor+=1
    if not re.fullmatch(r'\s*(?:,\s*(?:"\s*)?)?}\s*,?\s*',line[cursor:]):return None
    return values if set(values)=={'id','text'} else None


def response_items(raw,stats=None):
    """Salvage independent JSONL rows after a malformed row, once per span.

    Successfully decoded containers cover their nested contents. Recovery only
    accepts a complete physical line containing an explicit id/text record; it
    never searches for braces inside strings or guesses IDs by position.
    """
    raw=raw.strip();items=[];covered=[];pos=0
    while pos<len(raw):
        while pos<len(raw) and raw[pos].isspace():pos+=1
        fence=re.match(r'```(?:jsonl?|ndjson)?[ \t]*(?:\r?\n|$)',raw[pos:],re.IGNORECASE)
        if fence:pos+=fence.end();continue
        if items and raw[pos:pos+1]==',':pos+=1;continue
        start=pos
        try:item,pos=DECODER.raw_decode(raw,pos)
        except ValueError:
            if raw[pos:pos+1]=='[':
                cursor=pos+1;complete=[]
                while cursor<len(raw):
                    while cursor<len(raw) and raw[cursor].isspace():cursor+=1
                    begin=cursor
                    try:value,cursor=DECODER.raw_decode(raw,cursor)
                    except ValueError:break
                    complete.append(value);covered.append((begin,cursor))
                    while cursor<len(raw) and raw[cursor].isspace():cursor+=1
                    if raw[cursor:cursor+1]!=',':break
                    cursor+=1
                if complete:items.append(complete)
            break
        items.append(item);covered.append((start,pos))
    offset=0;span=0;recovered=repairs=0
    for physical in raw.splitlines(keepends=True):
        start=offset;offset+=len(physical)
        leading=len(physical)-len(physical.lstrip());begin=start+leading
        while span<len(covered) and covered[span][1]<=begin:span+=1
        if span<len(covered) and covered[span][0]<=begin<covered[span][1]:continue
        line=physical.strip()
        # A normal row delimiter is not part of its JSON value.
        strict=line[:-1].rstrip() if line.endswith(',') else line
        try:item=json.loads(strict,object_pairs_hook=unique_object)
        except ValueError:
            item=repair_row_tail(line)
            if item is None:continue
            repairs+=1
        if not isinstance(item,dict) or set(item)!={'id','text'}:continue
        if not isinstance(item['id'],str) or not isinstance(item['text'],str):continue
        items.append(item);recovered+=1
    if stats is not None:stats.update(recovered_rows=recovered,syntax_repairs=repairs)
    return items
