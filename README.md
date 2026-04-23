# api-server

Backend workspace for ParkTrack.

The repository currently keeps the legacy Python/FastAPI implementation in `src/`
and the new ASP.NET Core implementation in `ParkTrack.Api/`.

## ASP.NET Core API

```bash
dotnet run --project ParkTrack.Api/ParkTrack.Api.csproj
```

Implemented foundation endpoints:

- `GET /health`
- `GET /version`
