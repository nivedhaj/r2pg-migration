using Npgsql;
using StudentFeePoc.Db.Fee;
using Xunit;

namespace StudentFeePoc.Tests;

public class DbWriteTests : IClassFixture<TestFixture>
{
    private readonly TestFixture _fixture;

    public DbWriteTests(TestFixture fixture)
    {
        _fixture = fixture;
    }

    [Fact]
    public async Task CanInsertFeeTransactionAndVerifyPersistenceAndTimestampTrigger()
    {
        // 1. Arrange: Find an existing student or use a test Guid
        Guid studentId = Guid.NewGuid();
        Guid feeId = Guid.NewGuid();

        // Check if real student exists to attach transaction
        await using (var cmd = _fixture.DataSource.CreateCommand("SELECT id FROM student LIMIT 1;"))
        {
            var existingId = await cmd.ExecuteScalarAsync();
            if (existingId is Guid g)
            {
                studentId = g;
            }
        }

        // Check if real fee exists
        await using (var cmd = _fixture.DataSource.CreateCommand("SELECT id FROM fee LIMIT 1;"))
        {
            var existingFeeId = await cmd.ExecuteScalarAsync();
            if (existingFeeId is Guid fg)
            {
                feeId = fg;
            }
        }

        var request = new CreateFeeTransactionRequest(
            StudentId: studentId,
            Amount: 750.50m,
            Status: "Active",
            FeeId: feeId,
            RefNo: "TEST-REF-999",
            PaidBy: "Online Banking"
        );

        // 2. Act: Insert transaction
        var result = await FeeTransactionWriteQueries.InsertFeeTransactionAsync(_fixture.DataSource, request);

        // 3. Assert: Verify returned response
        Assert.NotNull(result);
        Assert.NotEqual(Guid.Empty, result.Id);
        Assert.Equal(750.50m, result.Amount);
        Assert.StartsWith("TXN-", result.TxNo);

        // 4. Assert: Direct DB Query verification in fee_transaction
        await using (var verifyCmd = _fixture.DataSource.CreateCommand("SELECT amount, status::text, ref_no FROM fee_transaction WHERE id = @id;"))
        {
            verifyCmd.Parameters.AddWithValue("id", result.Id);
            await using var reader = await verifyCmd.ExecuteReaderAsync();
            Assert.True(await reader.ReadAsync(), "Transaction was not found in fee_transaction table.");
            Assert.Equal(750.50m, reader.GetDecimal(0));
            Assert.Equal("Active", reader.GetString(1));
            Assert.Equal("TEST-REF-999", reader.GetString(2));
        }

        // 5. Assert: Verify trigger auto-updates modified_on timestamp on update
        await using (var updateCmd = _fixture.DataSource.CreateCommand("UPDATE fee_transaction SET ref_no = 'TEST-REF-UPDATED' WHERE id = @id;"))
        {
            updateCmd.Parameters.AddWithValue("id", result.Id);
            await updateCmd.ExecuteNonQueryAsync();
        }

        await using (var verifyUpdateCmd = _fixture.DataSource.CreateCommand("SELECT ref_no, modified_on FROM fee_transaction WHERE id = @id;"))
        {
            verifyUpdateCmd.Parameters.AddWithValue("id", result.Id);
            await using var reader = await verifyUpdateCmd.ExecuteReaderAsync();
            Assert.True(await reader.ReadAsync());
            Assert.Equal("TEST-REF-UPDATED", reader.GetString(0));
            Assert.False(reader.IsDBNull(1), "modified_on should be set by PostgreSQL trigger.");
        }

        // 6. Cleanup test records
        await using (var cleanupCmd = _fixture.DataSource.CreateCommand("DELETE FROM fee_transaction WHERE id = @id;"))
        {
            cleanupCmd.Parameters.AddWithValue("id", result.Id);
            await cleanupCmd.ExecuteNonQueryAsync();
        }
    }
}
