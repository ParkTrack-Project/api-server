using ParkTrack.Api.Endpoints;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddCors(options =>
{
    options.AddPolicy("ParkTrackClients", policy =>
    {
        policy
            .WithOrigins(
                "https://swagger.parktrack.live",
                "https://labeler.parktrack.live",
                "https://parktrack.live",
                "http://localhost:5173")
            .AllowAnyHeader()
            .AllowAnyMethod();
    });
});

var app = builder.Build();

app.UseCors("ParkTrackClients");

app.MapSystemEndpoints();

app.Run();
