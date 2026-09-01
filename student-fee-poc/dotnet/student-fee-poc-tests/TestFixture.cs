using Microsoft.Extensions.Configuration;
using Npgsql;

namespace StudentFeePoc.Tests;

public class TestFixture : IDisposable
{
    public NpgsqlDataSource DataSource { get; }
    public string ConnectionString { get; }

    public TestFixture()
    {
        var config = new ConfigurationBuilder()
            .SetBasePath(AppContext.BaseDirectory)
            .AddJsonFile("appsettings.json", optional: true)
            .AddEnvironmentVariables()
            .Build();

        ConnectionString = Environment.GetEnvironmentVariable("ConnectionStrings__Postgres")
            ?? config.GetConnectionString("Postgres")
            ?? "Host=localhost;Port=5422;Database=rpg;Username=postgres;Password=YourSecurePasswordHere";

        DataSource = NpgsqlDataSource.Create(ConnectionString);
    }

    public void Dispose()
    {
        DataSource.Dispose();
    }
}
