<?php

/**
 * Container definitions.
 *
 * Only the four bindings PHP-DI cannot work out on its own are here: two that
 * read the environment, one interface-to-implementation choice, and one PSR
 * factory. Controllers, services and repositories are autowired from their
 * constructor types, so adding a route means adding a class and nothing else.
 */

declare(strict_types=1);

use Memo\Database\ConnectionFactory;
use Memo\Storage\LocalStorage;
use Memo\Storage\Storage;
use Memo\Support\Config;
use Psr\Http\Message\ResponseFactoryInterface;
use Slim\Psr7\Factory\ResponseFactory;

return [
    Config::class => static fn (): Config => Config::fromEnvironment(),

    ConnectionFactory::class => static fn (Config $config): ConnectionFactory
        => new ConnectionFactory($config->databaseUrl),

    // The swap point named in MEMO-05: an S3 driver replaces this one line.
    Storage::class => static fn (Config $config): Storage => new LocalStorage($config->audioDir),

    // Needed by JsonErrorHandler, which has to build a response from outside the
    // routing path and so is not handed one.
    ResponseFactoryInterface::class => static fn (): ResponseFactoryInterface => new ResponseFactory(),
];
