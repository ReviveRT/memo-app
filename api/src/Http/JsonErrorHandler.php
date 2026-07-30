<?php

declare(strict_types=1);

namespace Memo\Http;

use Memo\Support\Stderr;
use Psr\Http\Message\ResponseFactoryInterface;
use Psr\Http\Message\ResponseInterface;
use Psr\Http\Message\ServerRequestInterface;
use Slim\Exception\HttpException;
use Slim\Exception\HttpMethodNotAllowedException;
use Slim\Interfaces\ErrorHandlerInterface;
use Throwable;

/**
 * Slim's default error handler content-negotiates and will happily answer an
 * unrouted /api/typo with an HTML error page. This API only speaks JSON, and the
 * frontend parses every response as JSON, so an HTML 404 surfaces in the browser
 * as an unexplained parse error instead of a 404.
 */
final class JsonErrorHandler implements ErrorHandlerInterface
{
    public function __construct(private readonly ResponseFactoryInterface $responseFactory)
    {
    }

    public function __invoke(
        ServerRequestInterface $request,
        Throwable $exception,
        bool $displayErrorDetails,
        bool $logErrors,
        bool $logErrorDetails,
    ): ResponseInterface {
        [$status, $message] = $this->describe($exception);

        // Only server faults are logged. A 404 or a 405 is a caller's mistake and
        // logging it lets an unauthenticated scan fill the container's logs.
        if ($status >= 500) {
            Stderr::write(sprintf(
                '[error] %s %s -> %d: %s: %s in %s:%d',
                $request->getMethod(),
                (string) $request->getUri()->getPath(),
                $status,
                $exception::class,
                $exception->getMessage(),
                $exception->getFile(),
                $exception->getLine(),
            ));
            Stderr::write($exception->getTraceAsString());
        }

        $response = Json::write(
            $this->responseFactory->createResponse($status),
            ['error' => ['status' => $status, 'message' => $message]],
            $status,
        );

        // A 405 without Allow is malformed per RFC 7231, and Slim puts the routed
        // methods on the exception rather than on the response for us.
        if ($exception instanceof HttpMethodNotAllowedException) {
            $response = $response->withHeader('Allow', implode(', ', $exception->getAllowedMethods()));
        }

        return $response;
    }

    /**
     * @return array{0: int, 1: string}
     */
    private function describe(Throwable $exception): array
    {
        if ($exception instanceof HttpException) {
            $status = $exception->getCode();

            // getCode() is typed as mixed on Throwable and only Slim's own
            // subclasses guarantee it holds the status, so it is range-checked
            // rather than trusted -- a 0 or a 200 here would produce a response
            // that claims success while carrying an error body.
            if (is_int($status) && $status >= 400 && $status <= 599) {
                return [$status, $exception->getMessage()];
            }

            return [500, $exception->getMessage()];
        }

        // The message of an arbitrary throwable can carry a query, a path or a
        // connection string, so it goes to the logs above and never to the client.
        return [500, 'Internal server error. See the api container logs for details.'];
    }
}
