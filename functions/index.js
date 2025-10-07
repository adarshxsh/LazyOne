
const functions = require("firebase-functions");
const admin = require("firebase-admin");
admin.initializeApp();

const BUCKET_NAME = 'YOUR_FIREBASE_STORAGE_BUCKET_URL'; // IMPORTANT: Replace with your bucket URL

/**
 * A Cloud Function that is scheduled to run once a day.
 * It scans the /chat_images/ directory in Firebase Storage and deletes
 * any file that is older than 30 days.
 */
exports.deleteOldImages = functions.pubsub.schedule('every 24 hours').onRun(async (context) => {
  const bucket = admin.storage().bucket(BUCKET_NAME);
  const [files] = await bucket.getFiles({ prefix: 'chat_images/' });

  const thirtyDaysAgo = new Date();
  thirtyDaysAgo.setDate(thirtyDaysAgo.getDate() - 30);

  const promises = [];
  let deletedCount = 0;

  files.forEach(file => {
    const fileCreationTime = new Date(file.metadata.timeCreated);

    if (fileCreationTime < thirtyDaysAgo) {
      console.log(`Deleting old file: ${file.name}`);
      promises.push(file.delete());
      deletedCount++;
    }
  });

  await Promise.all(promises);
  console.log(`Successfully deleted ${deletedCount} old images.`);
  return null;
});
