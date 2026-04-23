namespace ParkTrack.Api.Endpoints;

using System.Text.Json.Serialization;

public static class SystemEndpoints
{
    public static IEndpointRouteBuilder MapSystemEndpoints(this IEndpointRouteBuilder app)
    {
        app.MapGet("/health", (IConfiguration configuration) =>
        {
            var hasConnectionString = !string.IsNullOrWhiteSpace(
                configuration.GetConnectionString("Default"));

            return Results.Ok(new HealthResponse(
                Status: hasConnectionString ? "healthy" : "degraded",
                Database: hasConnectionString ? "connected" : "disconnected"));
        });

        app.MapGet("/version", () => Results.Ok(new VersionResponse("1.0")));

        return app;
    }
}

public sealed record HealthResponse(string Status, string Database);

public sealed record VersionResponse(
    [property: JsonPropertyName("api_version")] string ApiVersion);
