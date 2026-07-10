# Requirements Specification

## Functional Requirements
1. **User Sign Up & Profile**:  - [ ] 
   - Register via email/password or Google authentication.
   - Maintain a profile with details (college, hostel, major, roll number, hostel room number, bio).
   - Display a default of 1500 reward points upon registration.

2. **Tasks Platform**:  
   - Create a task specifying title, description, and positive integer reward points.
   - Restrict users from posting tasks if they have insufficient points.
   - Accept/take a task posted by another user.
   - Mark a taken task as completed to transfer points from the poster to the executor.
3. **Social Graph**: 
   - View, search, and send friend requests to other users.
   - View interactive visual network layout of verified friends.
   - Support a custom friend request closeness metric (range 0-100).
4. **Task-Specific Chats**: 
   - Start real-time chats automatically tied to specific tasks/conversations.
   - Notify users about updates.

## Non-Functional Requirements
- **Real-time Performance**: Messages must deliver via WebSocket with minimal latency.
- **Security**: Environment secrets (API keys, path credentials) must be loaded securely via `.env`
- **Portability**: Support deployment on AWS cloud infrastructure.
- **Task-Flow:** From more close friend to the lesser closer friend in the form of wave 

