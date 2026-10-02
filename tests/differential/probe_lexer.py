"""Ask Spark 3.5.1's own runtime lexer how it tokenizes an identifier.

The vendored 3.5.1 grammar declares `fragment LETTER : [A-Z]` with no
`options { caseInsensitive }`, which would make our ANTLR 4.13.1 Python port reject
`prod`. Real `spark.sql("drop table prod.users")` parses fine, and `SeLeCt 1` is
accepted too, so the shipped lexer must match lowercase somehow.

This loads Spark's own `SqlBaseLexer` from the running JVM. Token *names* are not
needed: a matched identifier comes back as ONE token spanning the whole word, while an
unrecognized character comes back one token per character. That is enough to tell
whether Spark's lexer is case-sensitive.
"""
from pyspark.sql import SparkSession

spark = (
    SparkSession.builder
    .master("local[1]")
    .appName("sparkscreen-lexer-probe")
    .config("spark.ui.enabled", "false")
    .getOrCreate()
)
spark.sparkContext.setLogLevel("ERROR")
jvm = spark._jvm
Clazz = jvm.java.lang.Class

CharStreams = Clazz.forName("org.antlr.v4.runtime.CharStreams")
CommonTokenStream = Clazz.forName("org.antlr.v4.runtime.CommonTokenStream")
LexerCls = Clazz.forName("org.apache.spark.sql.catalyst.parser.SqlBaseLexer")

print("lexer class:", LexerCls.getName())
try:
    for text in ["prod", "PROD", "SeLeCt", "x_1", "users", "table"]:
        lexer = LexerCls(CharStreams.fromString(text))
        tokens = CommonTokenStream(lexer)
        tokens.fill()
        pieces = [str(t.getText()) for t in tokens.getTokens()]
        whole = len(pieces) == 1 and pieces[0] == text
        print(f"  {text!r:10} -> {len(pieces)} token(s) {pieces} "
              f"{'MATCHED-AS-ONE' if whole else 'SPLIT/REJECTED'}")
finally:
    spark.stop()
