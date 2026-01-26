const express = require('express');
const router = express.Router();
const bcrypt = require('bcrypt');
const jwt = require('jsonwebtoken');
const User = require('../models/User');

// Signup endpoint
router.post('/signup', async (req, res) => {
  try {
    const { email, password } = req.body;

    // Normalize email
    const normalizedEmail = email.trim().toLowerCase();

    // Check if user already exists
    const existingUser = await User.findOne({ email: normalizedEmail });
    if (existingUser) {
      return res.status(400).json({ message: 'User already exists' });
    }

    // Hash password
    const hashedPassword = await bcrypt.hash(password, 10);

    // Create new user
    const newUser = new User({
      email: normalizedEmail,
      password: hashedPassword
    });

    await newUser.save();

    // Generate token
    const token = jwt.sign(
      { userId: newUser._id, email: newUser.email },
      process.env.JWT_SECRET,
      { expiresIn: '7d' }
    );

    res.status(201).json({
      message: 'Signup successful',
      token,
      user: { id: newUser._id, email: newUser.email }
    });
  } catch (error) {
    res.status(500).json({ message: 'Server error', error: error.message });
  }
});

// Login endpoint
router.post('/login', async (req, res) => {
  try {
    // Debug logging
    console.log('LOGIN REQUEST BODY:', req.body);

    // Handle both 'email' and 'username' fields from frontend
    const { email, username, password } = req.body;
    
    // Determine which field to use for login
    const loginEmail = email || username;
    
    console.log('LOGIN EMAIL RAW:', loginEmail);
    console.log('LOGIN PASSWORD PROVIDED:', password ? '[PRESENT]' : '[MISSING]');

    if (!loginEmail) {
      return res.status(400).json({ message: 'Email is required' });
    }

    if (!password) {
      return res.status(400).json({ message: 'Password is required' });
    }

    // Normalize email
    const normalizedEmail = loginEmail.trim().toLowerCase();
    console.log('LOGIN EMAIL NORMALIZED:', normalizedEmail);

    // Find user by email only
    const user = await User.findOne({ email: normalizedEmail });
    console.log('USER FOUND:', user ? 'YES' : 'NO');
    
    if (user) {
      console.log('USER EMAIL:', user.email);
      console.log('USER PASSWORD HASH:', user.password.substring(0, 20) + '...');
    }

    if (!user) {
      return res.status(400).json({ message: 'Invalid email or password' });
    }

    // Compare hashed password
    console.log('BCRYPT COMPARE STARTING...');
    const isPasswordValid = await bcrypt.compare(password, user.password);
    console.log('PASSWORD MATCH:', isPasswordValid);

    if (!isPasswordValid) {
      return res.status(400).json({ message: 'Invalid email or password' });
    }

    // Generate token
    const token = jwt.sign(
      { userId: user._id, email: user.email },
      process.env.JWT_SECRET,
      { expiresIn: '7d' }
    );

    console.log('LOGIN SUCCESSFUL FOR:', user.email);

    res.json({
      message: 'Login successful',
      token,
      user: { id: user._id, email: user.email }
    });
  } catch (error) {
    console.error('LOGIN ERROR:', error);
    res.status(500).json({ message: 'Server error', error: error.message });
  }
});

// Get current user
router.get('/me', async (req, res) => {
  try {
    const token = req.headers.authorization?.split(' ')[1];
    if (!token) {
      return res.status(401).json({ message: 'No token provided' });
    }

    const decoded = jwt.verify(token, process.env.JWT_SECRET);
    const user = await User.findById(decoded.userId).select('-password');
    
    if (!user) {
      return res.status(404).json({ message: 'User not found' });
    }

    res.json({ user });
  } catch (error) {
    res.status(401).json({ message: 'Invalid token' });
  }
});

module.exports = router;
