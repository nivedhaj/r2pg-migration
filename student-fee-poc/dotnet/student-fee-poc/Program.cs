using System.Text.Json;
using Microsoft.AspNetCore.Mvc;
using Npgsql;
using StudentFeePoc.Db.Fee;
using StudentFeePoc.Db.Student;
using StudentFeePoc.Models;

var builder = WebApplication.CreateBuilder(args);

// Read PostgreSQL connection string from config / env
var connectionString = builder.Configuration.GetConnectionString("Postgres");
if (string.IsNullOrWhiteSpace(connectionString))
{
    Console.Error.WriteLine("Missing ConnectionStrings:Postgres in configuration.");
    Environment.Exit(1);
}

// Register NpgsqlDataSource as Singleton
builder.Services.AddSingleton(NpgsqlDataSource.Create(connectionString));

// Register Swagger/OpenAPI
builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen(options =>
{
    options.SwaggerDoc("v1", new Microsoft.OpenApi.Models.OpenApiInfo
    {
        Title = "CampusTrack Parity API (Student & Fee Module)",
        Version = "v1",
        Description = "Parity REST APIs for Student and Fee modules matching CampusTrack product endpoints, powered by PostgreSQL 16 & ASP.NET Core 10."
    });
});

var app = builder.Build();

// Swagger UI configuration
app.UseSwagger();
app.UseSwaggerUI(c =>
{
    c.SwaggerEndpoint("/swagger/v1/swagger.json", "CampusTrack Parity API v1");
    c.RoutePrefix = string.Empty; // Set Swagger UI at app root (http://localhost:5000/)
});

// 1. Health Check Endpoint
app.MapGet("/health", async (NpgsqlDataSource dataSource) =>
{
    try
    {
        await using var cmd = dataSource.CreateCommand("SELECT 1;");
        await cmd.ExecuteScalarAsync();
        return Results.Ok(new { status = "Healthy", database = "Connected", timestamp = DateTime.UtcNow });
    }
    catch (Exception ex)
    {
        return Results.Json(new { status = "Unhealthy", database = "Disconnected", error = ex.Message }, statusCode: 503);
    }
})
.WithName("HealthCheck")
.WithTags("System");

// 2. CampusTrack Student Endpoint: GET /api/stu/student
app.MapGet("/api/stu/student", async (
    NpgsqlDataSource dataSource,
    [FromQuery] bool activeStudentsOnly = false,
    [FromQuery] bool todaysAbsenteesOnly = false,
    [FromQuery] int limit = 50) =>
{
    var response = await StudentQueries.GetStudentsPayloadAsync(
        dataSource,
        activeStudentsOnly: activeStudentsOnly,
        todaysAbsenteesOnly: todaysAbsenteesOnly,
        limit: limit
    );
    return Results.Ok(response);
})
.WithName("GetStudents")
.WithTags("Student Module");

// 3. CampusTrack Fee Transaction Endpoint: GET /api/feeTx
app.MapGet("/api/feeTx", async (
    NpgsqlDataSource dataSource,
    [FromQuery] Guid? id = null,
    [FromQuery] int limit = 10) =>
{
    var apiResponse = await FeeTransactionQueries.GetFeeTransactionsAsync(dataSource, id: id, limit: limit);
    return Results.Ok(apiResponse);
})
.WithName("GetFeeTransactions")
.WithTags("Fee Module");

// 4. CampusTrack Fee Transaction Write Endpoint: POST /api/feeTx
app.MapPost("/api/feeTx", async (
    NpgsqlDataSource dataSource,
    [FromBody] CreateFeeTransactionRequest request) =>
{
    try
    {
        var result = await FeeTransactionWriteQueries.InsertFeeTransactionAsync(dataSource, request);
        return Results.Created($"/api/feeTx?id={result.Id}", result);
    }
    catch (Exception ex)
    {
        return Results.BadRequest(new { error = ex.Message });
    }
})
.WithName("CreateFeeTransaction")
.WithTags("Fee Module");

// 5. CampusTrack Fee Definition Endpoint: GET /api/fee
app.MapGet("/api/fee", async (
    NpgsqlDataSource dataSource,
    [FromQuery] int limit = 50) =>
{
    var fees = await FeeQueries.GetFeesAsync(dataSource, limit: limit);
    return Results.Ok(fees);
})
.WithName("GetFees")
.WithTags("Fee Module");

app.Run();
