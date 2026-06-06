The goal is to setup a data warehouse environment for local use for experimentation in converting csv files iceberg tables and having some engine query the data for usage in a BI tool, such as Python's Dash.

I want a script that downloads the Adventure Works DW csv files.

I want to experiment using these three tools to read the csv files and convert them to Iceberg tables:
- Pyiceberg
- Apache Data Fusion
- DuckDB

The database should be called `adventure_works_dw`. 

The schema should be based on the tool name doing the loading (i.e. `pyiceberg`, `duckdb`, `datafusion`). The table name is whatever the table name actualy is.

Create a script that does that for each tool, so that I can do some benchmarking on speed and also assess what the code looks like.

I'm not sure what open source catalog is the best for Iceberg tables, so do some research and pick the optimal one.


Create a container for Trino, DuckDB, PosgresSQL (OLAP) that reads from the Iceberg tables. This is to experiment with different engines for connecting to the data with a BI tool, which will be Python's Dash.

Also, create another container for Druid and a means for keeping it up-to-date from the Iceberg tables.


In another project (not here), I'll be creating a Dash application that has connectors for Druid,  Trino, DuckDB, and Posgres and experiment with dashboard visualization peformances and such using each of them.

What questions do you have in order to set this up?