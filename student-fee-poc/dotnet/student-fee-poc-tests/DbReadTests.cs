using Npgsql;
using StudentFeePoc.Db.Fee;
using StudentFeePoc.Db.Student;
using Xunit;

namespace StudentFeePoc.Tests;

public class DbReadTests : IClassFixture<TestFixture>
{
    private readonly TestFixture _fixture;

    public DbReadTests(TestFixture fixture)
    {
        _fixture = fixture;
    }

    [Fact]
    public async Task CanConnectToDatabase()
    {
        await using var cmd = _fixture.DataSource.CreateCommand("SELECT 1;");
        var result = await cmd.ExecuteScalarAsync();
        Assert.Equal(1, Convert.ToInt32(result));
    }

    [Fact]
    public async Task CanQueryRecentStudentsWithEnrollments()
    {
        var students = await StudentQueries.GetRecentStudentsAsync(_fixture.DataSource, limit: 5);
        Assert.NotNull(students);
        // If data is migrated, verify properties
        if (students.Count > 0)
        {
            var first = students[0];
            Assert.NotEqual(Guid.Empty, first.Id);
        }
    }

    [Fact]
    public async Task CanQueryStudentFeeSummaryView()
    {
        const string sql = """
            SELECT id, student_id, student_name, course_name, fee_name, paid_amount
            FROM student_fee_summary_view
            LIMIT 5;
            """;

        await using var cmd = _fixture.DataSource.CreateCommand(sql);
        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            var pk = reader.GetGuid(0);
            Assert.NotEqual(Guid.Empty, pk);
        }
    }

    [Fact]
    public async Task CanQueryFeeTransactionsApiPayload()
    {
        var response = await FeeTransactionQueries.GetFeeTransactionsAsync(_fixture.DataSource, limit: 5);
        Assert.NotNull(response);
        Assert.NotNull(response.Data);
        Assert.NotNull(response.Data.Data);
    }
}
