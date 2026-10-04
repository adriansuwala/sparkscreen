/*
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * This file is an adaptation of Presto's presto-parser/src/main/antlr4/com/facebook/presto/sql/parser/SqlBase.g4 grammar.
 */
lexer grammar SqlBaseLexer;
options { caseInsensitive = true; }
@header {from collections import deque as _java_Deque

}
@members {
has_unclosed_bracketed_comment = False
complex_type_level_counter = 0

def _port_tags(self):
    d = self.__dict__.get("_port_tags_deque")
    if d is None:
        d = _java_Deque()
        self.__dict__["_port_tags_deque"] = d
    return d

def _port_text(self):
    # Java getText() from inside a lexer action; the Python Lexer has no such method
    return self._input.getText(self._tokenStartCharIndex, self._input.index - 1)

def isValidDecimal(self):
    nextChar = self._input.LA(1)
    if ord('A') <= nextChar <= ord('Z') or ord('0') <= nextChar <= ord('9') or nextChar == ord('_'):
        return False
    return True

def isHint(self):
    return self._input.LA(1) == ord('+')

def markUnclosedComment(self):
    self.has_unclosed_bracketed_comment = True

def incComplexTypeLevelCounter(self):
    self.complex_type_level_counter += 1

def decComplexTypeLevelCounter(self):
    if self.complex_type_level_counter > 0:
        self.complex_type_level_counter -= 1

def isShiftRightOperator(self):
    return self.complex_type_level_counter == 0

}

/*
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * This file is an adaptation of Presto's presto-parser/src/main/antlr4/com/facebook/presto/sql/parser/SqlBase.g4 grammar.
 */






SEMICOLON: ';';

LEFT_PAREN: '(';
RIGHT_PAREN: ')';
COMMA: ',';
DOT: '.';
LEFT_BRACKET: '[';
RIGHT_BRACKET: ']';
BANG: '!';

// NOTE: If you add a new token in the list below, you should update the list of keywords
// and reserved tag in `docs/sql-ref-ansi-compliance.md#sql-keywords`, and
// modify `ParserUtils.toExprAlias()` which assumes all keywords are between `ADD` and `ZONE`.

//============================
// Start of the keywords list
//============================
//--SPARK-KEYWORD-LIST-START
ADD: 'ADD';
AFTER: 'AFTER';
AGGREGATE: 'AGGREGATE';
ALL: 'ALL';
ALTER: 'ALTER';
ALWAYS: 'ALWAYS';
ANALYZE: 'ANALYZE';
AND: 'AND';
ANTI: 'ANTI';
ANY: 'ANY';
ANY_VALUE: 'ANY_VALUE';
APPROX: 'APPROX';
ARCHIVE: 'ARCHIVE';
ARRAY: 'ARRAY' {self.incComplexTypeLevelCounter();};
AS: 'AS';
ASC: 'ASC';
ASENSITIVE: 'ASENSITIVE';
AT: 'AT';
ATOMIC: 'ATOMIC';
AUTHORIZATION: 'AUTHORIZATION';
BEGIN: 'BEGIN';
BERNOULLI: 'BERNOULLI';
BETWEEN: 'BETWEEN';
BIGINT: 'BIGINT';
BINARY: 'BINARY';
BINDING: 'BINDING';
BOOLEAN: 'BOOLEAN';
BOTH: 'BOTH';
BUCKET: 'BUCKET';
BUCKETS: 'BUCKETS';
BY: 'BY';
BYTE: 'BYTE';
CACHE: 'CACHE';
CALL: 'CALL';
CALLED: 'CALLED';
CASCADE: 'CASCADE';
CASE: 'CASE';
CAST: 'CAST';
CATALOG: 'CATALOG';
CATALOGS: 'CATALOGS';
CHANGE: 'CHANGE';
CHANGES: 'CHANGES';
CHAR: 'CHAR';
CHARACTER: 'CHARACTER';
CHECK: 'CHECK';
CLEAR: 'CLEAR';
CLOSE: 'CLOSE';
CLUSTER: 'CLUSTER';
CLUSTERED: 'CLUSTERED';
CODEGEN: 'CODEGEN';
COLLATE: 'COLLATE';
COLLATION: 'COLLATION';
COLLATIONS: 'COLLATIONS';
COLLECTION: 'COLLECTION';
COLUMN: 'COLUMN';
COLUMNS: 'COLUMNS';
COMMENT: 'COMMENT';
COMMIT: 'COMMIT';
COMPACT: 'COMPACT';
COMPACTIONS: 'COMPACTIONS';
COMPENSATION: 'COMPENSATION';
COMPUTE: 'COMPUTE';
CONCATENATE: 'CONCATENATE';
CONDITION: 'CONDITION';
CONSTRAINT: 'CONSTRAINT';
CONTAINS: 'CONTAINS';
CONTINUE: 'CONTINUE';
COST: 'COST';
CREATE: 'CREATE';
CROSS: 'CROSS';
CUBE: 'CUBE';
CURRENT: 'CURRENT';
CURRENT_DATABASE: 'CURRENT_DATABASE';
CURRENT_DATE: 'CURRENT_DATE';
CURRENT_PATH: 'CURRENT_PATH';
CURRENT_SCHEMA: 'CURRENT_SCHEMA';
CURRENT_TIME: 'CURRENT_TIME';
CURRENT_TIMESTAMP: 'CURRENT_TIMESTAMP';
CURRENT_USER: 'CURRENT_USER';
CURSOR: 'CURSOR';
DAY: 'DAY';
DAYS: 'DAYS';
DAYOFYEAR: 'DAYOFYEAR';
DATA: 'DATA';
DATE: 'DATE';
DATABASE: 'DATABASE';
DATABASES: 'DATABASES';
DATEADD: 'DATEADD';
DATE_ADD: 'DATE_ADD';
DATEDIFF: 'DATEDIFF';
DATE_DIFF: 'DATE_DIFF';
DBPROPERTIES: 'DBPROPERTIES';
DEC: 'DEC';
DECIMAL: 'DECIMAL';
DECLARE: 'DECLARE';
DEFAULT: 'DEFAULT';
DEFAULT_PATH: 'DEFAULT_PATH';
DEFINED: 'DEFINED';
DEFINER: 'DEFINER';
DELAY: 'DELAY';
DELETE: 'DELETE';
DELIMITED: 'DELIMITED';
DESC: 'DESC';
DESCRIBE: 'DESCRIBE';
DETERMINISTIC: 'DETERMINISTIC';
DFS: 'DFS';
DIRECTORIES: 'DIRECTORIES';
DIRECTORY: 'DIRECTORY';
DISTANCE: 'DISTANCE';
DISTINCT: 'DISTINCT';
DISTRIBUTE: 'DISTRIBUTE';
DIV: 'DIV';
DO: 'DO';
DOUBLE: 'DOUBLE';
DROP: 'DROP';
ELSE: 'ELSE';
ELSEIF: 'ELSEIF';
END: 'END';
ENFORCED: 'ENFORCED';
ESCAPE: 'ESCAPE';
ESCAPED: 'ESCAPED';
EVOLUTION: 'EVOLUTION';
EXACT: 'EXACT';
EXCEPT: 'EXCEPT';
EXCHANGE: 'EXCHANGE';
EXCLUDE: 'EXCLUDE';
EXCLUSIVE: 'EXCLUSIVE';
EXISTS: 'EXISTS';
EXIT: 'EXIT';
EXPLAIN: 'EXPLAIN';
EXPORT: 'EXPORT';
EXTEND: 'EXTEND';
EXTENDED: 'EXTENDED';
EXTERNAL: 'EXTERNAL';
EXTRACT: 'EXTRACT';
FALSE: 'FALSE';
FETCH: 'FETCH';
FIELDS: 'FIELDS';
FILTER: 'FILTER';
FILEFORMAT: 'FILEFORMAT';
FIRST: 'FIRST';
FLOAT: 'FLOAT';
FLOW: 'FLOW';
FOLLOWING: 'FOLLOWING';
FOR: 'FOR';
FOREIGN: 'FOREIGN';
FORMAT: 'FORMAT';
FORMATTED: 'FORMATTED';
FOUND: 'FOUND';
FROM: 'FROM';
FULL: 'FULL';
FUNCTION: 'FUNCTION';
FUNCTIONS: 'FUNCTIONS';
GENERATED: 'GENERATED';
GEOGRAPHY: 'GEOGRAPHY';
GEOMETRY: 'GEOMETRY';
GLOBAL: 'GLOBAL';
GRANT: 'GRANT';
GROUP: 'GROUP';
GROUPING: 'GROUPING';
HANDLER: 'HANDLER';
HAVING: 'HAVING';
BINARY_HEX: 'X';
HOUR: 'HOUR';
HOURS: 'HOURS';
IDENTIFIER_KW: 'IDENTIFIER';
IDENTIFIED: 'IDENTIFIED';
IDENTITY: 'IDENTITY';
IF: 'IF';
IGNORE: 'IGNORE';
IMMEDIATE: 'IMMEDIATE';
IMPORT: 'IMPORT';
IN: 'IN';
INCLUDE: 'INCLUDE';
INCLUSIVE: 'INCLUSIVE';
INCREMENT: 'INCREMENT';
INDEX: 'INDEX';
INDEXES: 'INDEXES';
INNER: 'INNER';
INPATH: 'INPATH';
INPUT: 'INPUT';
INPUTFORMAT: 'INPUTFORMAT';
INSERT: 'INSERT';
INSENSITIVE: 'INSENSITIVE';
INTERSECT: 'INTERSECT';
INTERVAL: 'INTERVAL';
INT: 'INT';
INTEGER: 'INTEGER';
INTO: 'INTO';
INVOKER: 'INVOKER';
IS: 'IS';
ITEMS: 'ITEMS';
ITERATE: 'ITERATE';
JOIN: 'JOIN';
JSON: 'JSON';
KEY: 'KEY';
KEYS: 'KEYS';
LANGUAGE: 'LANGUAGE';
LAST: 'LAST';
LATERAL: 'LATERAL';
LAZY: 'LAZY';
LEADING: 'LEADING';
LEAVE: 'LEAVE';
LEFT: 'LEFT';
LEVEL: 'LEVEL';
LIKE: 'LIKE';
ILIKE: 'ILIKE';
LIMIT: 'LIMIT';
LINES: 'LINES';
LIST: 'LIST';
LOAD: 'LOAD';
LOCAL: 'LOCAL';
LOCATION: 'LOCATION';
LOCK: 'LOCK';
LOCKS: 'LOCKS';
LOGICAL: 'LOGICAL';
LONG: 'LONG';
LOOP: 'LOOP';
MACRO: 'MACRO';
MAP: 'MAP' {self.incComplexTypeLevelCounter();};
MATCHED: 'MATCHED';
MATERIALIZED: 'MATERIALIZED';
MAX: 'MAX';
MEASURE: 'MEASURE';
MERGE: 'MERGE';
METRICS: 'METRICS';
MICROSECOND: 'MICROSECOND';
MICROSECONDS: 'MICROSECONDS';
MILLISECOND: 'MILLISECOND';
MILLISECONDS: 'MILLISECONDS';
MINUTE: 'MINUTE';
MINUTES: 'MINUTES';
MODIFIES: 'MODIFIES';
MONTH: 'MONTH';
MONTHS: 'MONTHS';
MSCK: 'MSCK';
NAME: 'NAME';
NAMESPACE: 'NAMESPACE';
NAMESPACES: 'NAMESPACES';
NANOSECOND: 'NANOSECOND';
NANOSECONDS: 'NANOSECONDS';
NATURAL: 'NATURAL';
NEAREST: 'NEAREST';
NEXT: 'NEXT';
NO: 'NO';
NONE: 'NONE';
NOT: 'NOT';
NULL: 'NULL';
NULLS: 'NULLS';
NUMERIC: 'NUMERIC';
NORELY: 'NORELY';
OF: 'OF';
OFFSET: 'OFFSET';
ON: 'ON';
ONLY: 'ONLY';
OPEN: 'OPEN';
OPTION: 'OPTION';
OPTIONS: 'OPTIONS';
OR: 'OR';
ORDER: 'ORDER';
OUT: 'OUT';
OUTER: 'OUTER';
OUTPUTFORMAT: 'OUTPUTFORMAT';
OVER: 'OVER';
OVERLAPS: 'OVERLAPS';
OVERLAY: 'OVERLAY';
OVERWRITE: 'OVERWRITE';
PARTITION: 'PARTITION';
PARTITIONED: 'PARTITIONED';
PARTITIONS: 'PARTITIONS';
PATH: 'PATH';
PERCENTLIT: 'PERCENT';
PIVOT: 'PIVOT';
PLACING: 'PLACING';
POSITION: 'POSITION';
PRECEDING: 'PRECEDING';
PRIMARY: 'PRIMARY';
PRINCIPALS: 'PRINCIPALS';
PROCEDURE: 'PROCEDURE';
PROCEDURES: 'PROCEDURES';
PROPERTIES: 'PROPERTIES';
PURGE: 'PURGE';
QUALIFY: 'QUALIFY';
QUARTER: 'QUARTER';
QUERY: 'QUERY';
RANGE: 'RANGE';
READ: 'READ';
READS: 'READS';
REAL: 'REAL';
RECORDREADER: 'RECORDREADER';
RECORDWRITER: 'RECORDWRITER';
RECOVER: 'RECOVER';
RECURSION: 'RECURSION';
RECURSIVE: 'RECURSIVE';
REDUCE: 'REDUCE';
REFERENCES: 'REFERENCES';
REFRESH: 'REFRESH';
RELY: 'RELY';
RENAME: 'RENAME';
REPAIR: 'REPAIR';
REPEAT: 'REPEAT';
REPEATABLE: 'REPEATABLE';
REPLACE: 'REPLACE';
RESET: 'RESET';
RESPECT: 'RESPECT';
RESTRICT: 'RESTRICT';
RETURN: 'RETURN';
RETURNS: 'RETURNS';
REVOKE: 'REVOKE';
RIGHT: 'RIGHT';
RLIKE: 'RLIKE' | 'REGEXP';
ROLE: 'ROLE';
ROLES: 'ROLES';
ROLLBACK: 'ROLLBACK';
ROLLUP: 'ROLLUP';
ROW: 'ROW';
ROWS: 'ROWS';
SECOND: 'SECOND';
SECONDS: 'SECONDS';
SCHEMA: 'SCHEMA';
SCHEMAS: 'SCHEMAS';
SECURITY: 'SECURITY';
SELECT: 'SELECT';
SEMI: 'SEMI';
SEPARATED: 'SEPARATED';
SERDE: 'SERDE';
SERDEPROPERTIES: 'SERDEPROPERTIES';
SESSION_USER: 'SESSION_USER';
SET: 'SET';
SETMINUS: 'MINUS';
SETS: 'SETS';
SHORT: 'SHORT';
SHOW: 'SHOW';
SIMILARITY: 'SIMILARITY';
SINGLE: 'SINGLE';
SKEWED: 'SKEWED';
SMALLINT: 'SMALLINT';
SOME: 'SOME';
SORT: 'SORT';
SORTED: 'SORTED';
SOURCE: 'SOURCE';
SPECIFIC: 'SPECIFIC';
SQL: 'SQL';
SQLEXCEPTION: 'SQLEXCEPTION';
SQLSTATE: 'SQLSTATE';
START: 'START';
STATISTICS: 'STATISTICS';
STORED: 'STORED';
STRATIFY: 'STRATIFY';
STREAM: 'STREAM';
STREAMING: 'STREAMING';
STRING: 'STRING';
STRUCT: 'STRUCT' {self.incComplexTypeLevelCounter();};
SUBSTR: 'SUBSTR';
SUBSTRING: 'SUBSTRING';
SYNC: 'SYNC';
SYSTEM: 'SYSTEM';
SYSTEM_TIME: 'SYSTEM_TIME';
SYSTEM_VERSION: 'SYSTEM_VERSION';
SYSTEM_PATH: 'SYSTEM_PATH';
TABLE: 'TABLE';
TABLES: 'TABLES';
TABLESAMPLE: 'TABLESAMPLE';
TARGET: 'TARGET';
TBLPROPERTIES: 'TBLPROPERTIES';
TEMPORARY: 'TEMPORARY' | 'TEMP';
TERMINATED: 'TERMINATED';
THEN: 'THEN';
TIME: 'TIME';
TIMEDIFF: 'TIMEDIFF';
TIMESTAMP: 'TIMESTAMP';
TIMESTAMP_LTZ: 'TIMESTAMP_LTZ';
TIMESTAMP_NTZ: 'TIMESTAMP_NTZ';
TIMESTAMPADD: 'TIMESTAMPADD';
TIMESTAMPDIFF: 'TIMESTAMPDIFF';
TINYINT: 'TINYINT';
TO: 'TO';
EXECUTE: 'EXECUTE';
TOUCH: 'TOUCH';
TRAILING: 'TRAILING';
TRANSACTION: 'TRANSACTION';
TRANSACTIONS: 'TRANSACTIONS';
TRANSFORM: 'TRANSFORM';
TRIM: 'TRIM';
TRUE: 'TRUE';
TRUNCATE: 'TRUNCATE';
TRY_CAST: 'TRY_CAST';
TYPE: 'TYPE';
UNARCHIVE: 'UNARCHIVE';
UNBOUNDED: 'UNBOUNDED';
UNCACHE: 'UNCACHE';
UNION: 'UNION';
UNIQUE: 'UNIQUE';
UNKNOWN: 'UNKNOWN';
UNLOCK: 'UNLOCK';
UNPIVOT: 'UNPIVOT';
UNSET: 'UNSET';
UNTIL: 'UNTIL';
UPDATE: 'UPDATE';
USE: 'USE';
USER: 'USER';
USING: 'USING';
VALUE: 'VALUE';
VALUES: 'VALUES';
VARCHAR: 'VARCHAR';
VAR: 'VAR';
VARIABLE: 'VARIABLE';
VARIANT: 'VARIANT';
VERSION: 'VERSION';
VIEW: 'VIEW';
VIEWS: 'VIEWS';
VOID: 'VOID';
WATERMARK: 'WATERMARK';
WEEK: 'WEEK';
WEEKS: 'WEEKS';
WHEN: 'WHEN';
WHERE: 'WHERE';
WHILE: 'WHILE';
WINDOW: 'WINDOW';
WITH: 'WITH';
WITHIN: 'WITHIN';
WITHOUT: 'WITHOUT';
YEAR: 'YEAR';
YEARS: 'YEARS';
ZONE: 'ZONE';
//--SPARK-KEYWORD-LIST-END
//============================
// End of the keywords list
//============================

EQ  : '=' | '==';
NSEQ: '<=>';
NEQ : '<>';
NEQJ: '!=';
LT  : '<';
LTE : '<=' | '!>';
GT  : '>' {self.decComplexTypeLevelCounter();};
GTE : '>=' | '!<';
SHIFT_LEFT: '<<';
SHIFT_RIGHT: '>>' {self.isShiftRightOperator()}?;
SHIFT_RIGHT_UNSIGNED: '>>>' {self.isShiftRightOperator()}?;

PLUS: '+';
MINUS: '-';
ASTERISK: '*';
SLASH: '/';
PERCENT: '%';
TILDE: '~';
AMPERSAND: '&';
PIPE: '|';
CONCAT_PIPE: '||';
OPERATOR_PIPE: '|>';
HAT: '^';
COLON: ':';
DOUBLE_COLON: '::';
ARROW: '->';
FAT_ARROW : '=>';
HENT_START: '/*+';
HENT_END: '*/';
QUESTION: '?';

STRING_LITERAL
    : '\'' ( ~('\''|'\\') | ('\\' .) | ('\'' '\'') )* '\''
    | 'R\'' (~'\'')* '\''
    | 'R"'(~'"')* '"'
    ;

BEGIN_DOLLAR_QUOTED_STRING
    : DOLLAR_QUOTED_TAG {self._port_tags().append(self._port_text());} -> pushMode(DOLLAR_QUOTED_STRING_MODE)
    ;

DOUBLEQUOTED_STRING
    :'"' ( ~('"'|'\\') | '""' | ('\\' .) )* '"'
    ;

// NOTE: If you move a numeric literal, you should modify `ParserUtils.toExprAlias()`
// which assumes all numeric literals are between `BIGINT_LITERAL` and `BIGDECIMAL_LITERAL`.

BIGINT_LITERAL
    : DIGIT+ 'L'
    ;

SMALLINT_LITERAL
    : DIGIT+ 'S'
    ;

TINYINT_LITERAL
    : DIGIT+ 'Y'
    ;

INTEGER_VALUE
    : DIGIT+
    ;

EXPONENT_VALUE
    : DIGIT+ EXPONENT
    | DECIMAL_DIGITS EXPONENT {self.isValidDecimal()}?
    ;

DECIMAL_VALUE
    : DECIMAL_DIGITS {self.isValidDecimal()}?
    ;

FLOAT_LITERAL
    : DIGIT+ EXPONENT? 'F'
    | DECIMAL_DIGITS EXPONENT? 'F' {self.isValidDecimal()}?
    ;

DOUBLE_LITERAL
    : DIGIT+ EXPONENT? 'D'
    | DECIMAL_DIGITS EXPONENT? 'D' {self.isValidDecimal()}?
    ;

BIGDECIMAL_LITERAL
    : DIGIT+ EXPONENT? 'BD'
    | DECIMAL_DIGITS EXPONENT? 'BD' {self.isValidDecimal()}?
    ;

// Generalize the identifier to give a sensible INVALID_IDENTIFIER error message:
// * Unicode letters rather than a-z and A-Z only
// * URI paths for table references using paths
// We then narrow down to ANSI rules in exitUnquotedIdentifier() in the parser.
IDENTIFIER
    : (UNICODE_LETTER | DIGIT | '_')+
    | UNICODE_LETTER+ '://' (UNICODE_LETTER | DIGIT | '_' | '/' | '-' | '.' | '?' | '=' | '&' | '#' | '%')+
    ;

BACKQUOTED_IDENTIFIER
    : '`' ( ~'`' | '``' )* '`'
    ;

fragment DECIMAL_DIGITS
    : DIGIT+ '.' DIGIT*
    | '.' DIGIT+
    ;

fragment EXPONENT
    : 'E' [+-]? DIGIT+
    ;

fragment DIGIT
    : [0-9]
    ;

fragment LETTER
    : [A-Z]
    ;

fragment DOLLAR_QUOTED_TAG
    : '$' LETTER* '$'
    ;

fragment UNICODE_LETTER
    : [\p{L}]
    ;

SIMPLE_COMMENT
    : '--' ('\\\n' | ~[\r\n])* '\r'? '\n'? -> channel(HIDDEN)
    ;

BRACKETED_COMMENT
    : '/*' {not self.isHint()}? ( BRACKETED_COMMENT | . )*? ('*/' | {self.markUnclosedComment();} EOF) -> channel(HIDDEN)
    ;

WS
    : [ \t\n\f\r\u000B\u00A0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200A\u2028\u202F\u205F\u3000]+ -> channel(HIDDEN)
    ;

// Catch-all for anything we can't recognize.
// We use this to be able to ignore and recover all the text
// when splitting statements with DelimiterLexer
UNRECOGNIZED
    : .
    ;

mode DOLLAR_QUOTED_STRING_MODE;
DOLLAR_QUOTED_STRING_BODY
    : ~'$'+
    | '$' ~'$'*
    ;

END_DOLLAR_QUOTED_STRING
    : DOLLAR_QUOTED_TAG {self._port_text() == self._port_tags()[-1]}? {self._port_tags().pop();} -> popMode
    ;
