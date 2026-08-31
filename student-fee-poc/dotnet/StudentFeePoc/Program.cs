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
        Title = "Student Fee POC API",
        Version = "v1",
        Description = "Demonstrating Module DB Access, API-Parity Cross-Module Views (student_fee_summary_view), and Trigger Audit."
    });
});

var app = builder.Build();

// Swagger UI configuration
app.UseSwagger();
app.UseSwaggerUI(c =>
{
    c.SwaggerEndpoint("/swagger/v1/swagger.json", "Student Fee POC API v1");
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

// 2. Single-Module: Student Query (Reads JSONB enrollments)
app.MapGet("/api/students", async (NpgsqlDataSource dataSource, [FromQuery] int limit = 10) =>
{
    var students = await StudentQueries.GetRecentStudentsAsync(dataSource, limit: limit);
    return Results.Ok(students);
})
.WithName("GetStudents")
.WithTags("Student Module");

// 3. Single-Module: Fee Query
app.MapGet("/api/fees", async (NpgsqlDataSource dataSource, [FromQuery] int limit = 10) =>
{
    var fees = await FeeQueries.GetRecentFeesAsync(dataSource, limit: limit);
    return Results.Ok(fees);
})
.WithName("GetFees")
.WithTags("Fee Module");

// 4. Synchronous Cross-Module View Query: student_fee_summary_view (Full API-Parity Tabular Dataset)
app.MapGet("/api/student-fees", async (NpgsqlDataSource dataSource, [FromQuery] int limit = 10) =>
{
    const string sql = """
        SELECT
            id,
            owner_id,
            parent_id,
            tx_no,
            to_char(tx_date AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS') AS tx_date,
            student_id,
            student_code,
            student_name,
            course_id,
            course_name,
            section,
            term_name,
            amount,
            fee_id,
            fee_name,
            fee_display_text,
            installment_id,
            installment_description,
            due_date,
            paid_amount,
            COALESCE(fines_paid, '[]'::jsonb)::text AS fines_paid_json,
            COALESCE(discounts, '[]'::jsonb)::text AS discounts_json,
            COALESCE(fee_adjustment, '{}'::jsonb)::text AS fee_adjustment_json,
            is_fine_paid,
            is_discount_given,
            is_opening_balance_adjusted,
            status,
            status_code,
            ref_no,
            paid_by
        FROM student_fee_summary_view
        ORDER BY tx_date DESC NULLS LAST, tx_no DESC
        LIMIT @limit;
        """;

    var rows = new List<StudentFeeViewModel>();
    await using var command = dataSource.CreateCommand(sql);
    command.Parameters.AddWithValue("limit", limit);

    await using var reader = await command.ExecuteReaderAsync();
    while (await reader.ReadAsync())
    {
        var finesJson = reader.GetString(20);
        var discountsJson = reader.GetString(21);
        var feeAdjJson = reader.GetString(22);

        using var finesDoc = JsonDocument.Parse(finesJson);
        using var discountsDoc = JsonDocument.Parse(discountsJson);
        using var feeAdjDoc = JsonDocument.Parse(feeAdjJson);

        rows.Add(new StudentFeeViewModel(
            reader.GetGuid(0),
            reader.IsDBNull(1) ? null : reader.GetGuid(1),
            reader.IsDBNull(2) ? null : reader.GetGuid(2),
            reader.IsDBNull(3) ? null : reader.GetString(3),
            reader.IsDBNull(4) ? null : reader.GetString(4),
            reader.IsDBNull(5) ? null : reader.GetGuid(5),
            reader.IsDBNull(6) ? null : reader.GetString(6),
            reader.IsDBNull(7) ? null : reader.GetString(7),
            reader.IsDBNull(8) ? null : reader.GetString(8),
            reader.IsDBNull(9) ? null : reader.GetString(9),
            reader.IsDBNull(10) ? null : reader.GetString(10),
            reader.IsDBNull(11) ? null : reader.GetString(11),
            reader.GetDecimal(12),
            reader.IsDBNull(13) ? null : reader.GetGuid(13),
            reader.IsDBNull(14) ? null : reader.GetString(14),
            reader.IsDBNull(15) ? null : reader.GetString(15),
            reader.IsDBNull(16) ? null : reader.GetInt32(16),
            reader.IsDBNull(17) ? null : reader.GetString(17),
            reader.IsDBNull(18) ? null : reader.GetString(18),
            reader.GetDecimal(19),
            finesDoc.RootElement.Clone(),
            discountsDoc.RootElement.Clone(),
            feeAdjDoc.RootElement.Clone(),
            reader.GetBoolean(23),
            reader.GetBoolean(24),
            reader.GetBoolean(25),
            reader.IsDBNull(26) ? null : reader.GetString(26),
            reader.GetInt32(27),
            reader.IsDBNull(28) ? null : reader.GetString(28),
            reader.IsDBNull(29) ? null : reader.GetString(29)
        ));
    }

    return Results.Ok(new
    {
        source = "student_fee_summary_view",
        count = rows.Count,
        data = rows
    });
})
.WithName("GetStudentFeeSummary")
.WithTags("Cross-Module Views");

// 5. Exact Fee Transactions API Response (PostgreSQL-Derived)
app.MapGet("/api/fee-transactions", async (NpgsqlDataSource dataSource, [FromQuery] int limit = 10) =>
{
    var apiResponse = await FeeTransactionQueries.GetFeeTransactionsAsync(dataSource, limit: limit);
    return Results.Ok(apiResponse);
})
.WithName("GetFeeTransactions")
.WithTags("Fee Module");

// 6. Write Endpoint: Insert Fee Transaction (Triggers Audit Log & Timestamp)
app.MapPost("/api/fee-transactions", async (NpgsqlDataSource dataSource, [FromBody] CreateFeeTransactionRequest request) =>
{
    try
    {
        var result = await FeeTransactionWriteQueries.InsertFeeTransactionAsync(dataSource, request);
        return Results.Created($"/api/fee-transactions/{result.Id}", result);
    }
    catch (Exception ex)
    {
        return Results.BadRequest(new { error = ex.Message });
    }
})
.WithName("CreateFeeTransaction")
.WithTags("Fee Module");

app.Run();
