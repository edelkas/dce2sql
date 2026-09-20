# dce2sql instructions

This file contains the instructions to plan the building of `dce2sql`.

## Table of contents

- [Summary](#summary)
- [JSON exports](#json-exports)
- [SQL database](#sql-database)
- [TODO](#todo)
- [The CLI](#the-cli)
- [Documentation](#documentation)

## Summary

**dce2sql** is a Python CLI tool that migrates Discord chat JSON exports to SQL databases, with tables for guilds, channels, users, messages, etc. The exports come from [DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter) (from now on, **DCE**), one of the main Discord archiving tools out there, which allows exporting to multiple formats, including JSON, which is the most suitable one for this task. They may also come from [my own fork](https://github.com/edelkas/DiscordChatExporter), which in particular adds extended exported data, so the tool needs to support both.

## SQL database

The tool should abstract away all actual database operations in order to be engine-agnostic, that way we can implement multiple SQL engines, which could be chosen by the user at migration time. I suggest we make an abstract class (ABC) for these adaptors, and each subclass implements an engine. The adaptor object can then be passed to the migration function, which would allow advanced users to even inject their own adaptors easily by subclassing suitably. Initially we will only implement a single engine - SQLite - so that the result can be stored in a single self-contained file. At the end we can extend the support to MySQL and PostgreSQL. Since SQLite doesn't have native timestamp or boolean data types, we shall use integers to store the UNIX timestamps or 0/1, respectively.

The migrations should be incremental: if the output database doesn't already exist (in the server, for engines like MySQL, or as a file, for engines like SQLite), it is created and everything is imported. But if it exists already, then the contents should instead be merged and updated. The merge policy is the following one: new records (guilds, channels, messages, etc) are simply inserted into the database. Pre-existing records have their fields updated instead, but only if there are actual changes (e.g. a channel's topic was changed or a message's content was edited, the guild's icon was substituted, etc). Deleted records (no longer present in the JSON exports) will never be deleted from the database, they will remain there for archival reasons. The tables that map to actual Discord objects will have two timestamp fields to keep track of these changes, with the usual naming convention: `created_at` and `updated_at`. Obviously, the former will point to when the record was created in the database, and will never be changed afterwards; whereas the latter will point to when the record was last updated, and will be changed every time the record is updated. That's why it's important that a record is only updated when any field has actually changed.

The precise schema of the database is detailed in [SQL.md](SQL.md).

## General considerations

For performance, during the migration of each JSON file, data should be batched as much as possible to reduce the amount of SQL statements required, and thus the overhead. For this, a pre-processing step in which data is first scanned is crucial, in order to determine how to batch data without breaking dependency relationships. For instance, the guild object, the channel object, and the user objects, don't functionally depend on other objects, so they can be updated in bulk at first. All users appearing in the file can first be fetched from the database by ID with a single request, then the new ones can be inserted with another one, and changes for pre-existing ones (e.g. in the display name, in the color, etc) can be applied with a further single update request. Similar logic should be applied for the remaining ones. Messages, in particular, should be handled with particular care, because they have cross-dependencies with many other tables (users, attachments, mentions, reactions, emojis, stickers, embeds, etc), and they have their history stored separately to prevent losing edits.

Since the process may nevertheless take a long time, there should be a status bar latched at the bottom of the terminal logging the current progress and being updated once per second. It shall indicate the number of messages parsed from the JSON exports thus far, the time spent and the ETA. The ETA can be estimated from the parsing speed and the total MB remaining, which is known in advance by adding up the sizes of the JSON exports even before parsing them. At the end of the process, verbose final stats should be printed: total number of each object parsed (users, messages, etc), number of existing messages edited, number of users deleted, total time taken, and many more to provide a wide statistical overview of the data.

Regarding the coding style, the tool need not remain dependency-less. If a library is a known robust choice to perform a certain task we may use it instead of reinventing the wheel and wasting tokens. Code should be well-documented and split in multiple files sensibly (e.g. one file per SQL engine); constants shall be abstracted at the top of each file instead of peppering magic values in the code.

## The CLI

Regarding the CLI, it should have the following mandatory positional arguments: the database name, and the JSON sources. Multiple sources can be listed, and glob patterns are supported. User exports from my fork should also be supported as valid sources.

It should also have at least the following optional parameters: one to select the SQL engine to use (SQLite by default), one to only list the JSON files that will be processed, and one to perform a dry run (which only parses the JSON files and prints some statistics about them, but without database queries). You may also add any additional parameters you deem necessary during the development in order to properly and flexibly configure the migration process, as well as standard ones such as one to show the help. For instance, when we implement additional engines (e.g. MySQL), we will surely require additional parameters for the server's user and password.

## Documentation

Documentation-wise, do generate a `README.md` file, however, be brief here, we don't want this to be overwhelming for users who just want to know how to use the tool. Just add the following sections: a summary, the main feature list, the usage, a couple examples, and references (Discord API reference, DCE itself, etc). Additional .md files can be added to the /docs folder for further details about the technical specification, program architecture, or anything else you deem useful. Also add a license file in the root of the project with the MIT license.