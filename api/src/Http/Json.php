<?php

declare(strict_types=1);

namespace Memo\Http;

use JsonException;
use Psr\Http\Message\ResponseInterface;
use RuntimeException;

/**
 * The single JSON encoding point, so every response -- success or error -- carries
 * the same content type and the same encoding flags.
 */
final class Json
{
    /**
     * @param array<string, mixed> $payload
     */
    public static function write(ResponseInterface $response, array $payload, int $status = 200): ResponseInterface
    {
        try {
            // THROW_ON_ERROR because the default is to return the string "false"
            // with a 200, which is a corrupt response the client cannot detect.
            // UNESCAPED_UNICODE keeps transcripts readable rather than \uXXXX --
            // memos are dictated speech and will not be ASCII.
            $body = json_encode(
                $payload,
                JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE,
            );
        } catch (JsonException $e) {
            throw new RuntimeException("Cannot encode response as JSON: {$e->getMessage()}", previous: $e);
        }

        $response->getBody()->write($body);

        return $response
            ->withStatus($status)
            ->withHeader('Content-Type', 'application/json; charset=utf-8');
    }
}
