<?php

declare(strict_types=1);

namespace Memo;

use DI\ContainerBuilder;
use Memo\Http\JsonErrorHandler;
use Slim\App;
use Slim\Factory\AppFactory;

/**
 * Assembles the application: container, middleware, routes. Kept out of
 * public/index.php so the front controller stays a single line and so a test or a
 * CLI command can build the same app.
 */
final class Kernel
{
    public static function createApp(): App
    {
        $root = dirname(__DIR__);

        $builder = new ContainerBuilder();
        $builder->addDefinitions($root . '/app/dependencies.php');
        $container = $builder->build();

        AppFactory::setContainer($container);
        $app = AppFactory::create();

        // Order matters, and this is the order from Slim's own documentation.
        // Middleware runs in reverse registration order, so the error middleware
        // registered last is the outermost layer -- which is the only position from
        // which it can catch a routing failure and turn it into a JSON 404.
        $app->addBodyParsingMiddleware();
        $app->addRoutingMiddleware();

        $errorMiddleware = $app->addErrorMiddleware(
            // Details never go into a response body. They would leak paths and
            // queries to any caller, and JsonErrorHandler already writes the whole
            // throwable to stderr, where `docker compose logs api` finds it.
            displayErrorDetails: false,
            // Both off: logging is JsonErrorHandler's job, and Slim's own logger
            // would duplicate every entry through error_log().
            logErrors: false,
            logErrorDetails: false,
        );
        $errorMiddleware->setDefaultErrorHandler($container->get(JsonErrorHandler::class));

        (require $root . '/app/routes.php')($app);

        return $app;
    }
}
